from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import config
from config import ExchangeConfig
from database.database import db as runtime_db
from services.binance_user_data_stream import BinanceFuturesUserDataStream
from services.credential_vault import CredentialVault

logger = logging.getLogger(__name__)


def _compact_symbol(symbol: str) -> str:
    return str(symbol or "").replace("/", "").replace(":", "").replace("-", "").upper()


class LiveExecutionService:
    """Camada de execucao real com persistencia local e reconciliacao com a exchange."""

    _TIMESTAMP_ERROR_MARKERS = (
        "-1021",
        "outside of the recvwindow",
        "timestamp for this request",
    )

    def __init__(self, database=None, credential_vault: Optional[CredentialVault] = None):
        self.database = database or runtime_db
        self.credential_vault = credential_vault or CredentialVault(strict=False)

    def is_ready(self) -> bool:
        return bool(self.credential_vault and self.credential_vault.is_configured())

    def _resolve_testnet(self, testnet: Optional[bool] = None) -> bool:
        return bool(config.TESTNET if testnet is None else testnet)

    @staticmethod
    def _exchange_request_params(
        extra: Optional[Dict[str, Any]] = None,
        *,
        exchange_name: str = "binanceusdm",
    ) -> Dict[str, Any]:
        params = dict(extra or {})
        normalized_exchange = ExchangeConfig.normalize_exchange_name(exchange_name)
        client_order_id = str(params.pop("clientOrderId", "") or params.pop("newClientOrderId", "") or "").strip()
        if client_order_id:
            if normalized_exchange == "binanceusdm":
                params.setdefault("newClientOrderId", client_order_id)
            else:
                params.setdefault("clientOrderId", client_order_id)
        recv_window = int(getattr(config, "BINANCE_RECV_WINDOW_MS", 60000) or 60000)
        if normalized_exchange == "binanceusdm" and recv_window > 0:
            params.setdefault("recvWindow", recv_window)
        return params

    @staticmethod
    def _uses_env_credentials(context: Dict[str, Any]) -> bool:
        source = str(context.get("credential_source") or "").strip().lower()
        return bool(context.get("use_env_credentials")) or source == "env"

    @staticmethod
    def _load_env_credentials(context: Dict[str, Any]) -> Dict[str, str]:
        exchange_name = ExchangeConfig.normalize_exchange_name(
            str(context.get("exchange_name") or context.get("exchange") or "binanceusdm")
        )
        if exchange_name == "bybit":
            api_key_env = "BYBIT_TESTNET_API_KEY" if config.TESTNET else "BYBIT_API_KEY"
            api_secret_env = "BYBIT_TESTNET_SECRET_KEY" if config.TESTNET else "BYBIT_SECRET_KEY"
            fallback_api_key_env = "BYBIT_API_KEY"
            fallback_api_secret_env = "BYBIT_SECRET_KEY"
        else:
            api_key_env = "BINANCE_TESTNET_API_KEY" if config.TESTNET else "BINANCE_API_KEY"
            api_secret_env = "BINANCE_TESTNET_SECRET_KEY" if config.TESTNET else "BINANCE_SECRET_KEY"
            fallback_api_key_env = "BINANCE_API_KEY"
            fallback_api_secret_env = "BINANCE_SECRET_KEY"
        api_key = str(context.get("api_key") or os.getenv(api_key_env, "") or os.getenv(fallback_api_key_env, "")).strip()
        api_secret = str(
            context.get("api_secret") or os.getenv(api_secret_env, "") or os.getenv(fallback_api_secret_env, "")
        ).strip()
        if not api_key or not api_secret:
            raise RuntimeError(
                f"Execucao live do runner exige {api_key_env} e {api_secret_env} configurados."
            )
        return {
            "user_id": int(context.get("user_id", 0) or 0),
            "account_id": str(context.get("account_id") or "env-primary"),
            "exchange": exchange_name,
            "api_key_ref": "env_api_key",
            "token_ref": "env_secret_key",
            "credential_alias": str(context.get("account_alias") or context.get("account_id") or "env-primary"),
            "credential_source": "env",
            "api_key": api_key,
            "api_secret": api_secret,
        }

    @classmethod
    def _is_timestamp_sync_error(cls, exc: Exception) -> bool:
        error_text = str(exc or "").lower()
        return any(marker in error_text for marker in cls._TIMESTAMP_ERROR_MARKERS)

    @staticmethod
    def _refresh_exchange_time_difference(exchange) -> None:
        if hasattr(exchange, "load_time_difference"):
            exchange.load_time_difference()

    def _call_signed_exchange(self, exchange, method_name: str, *args, **kwargs):
        method = getattr(exchange, method_name)
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            if not self._is_timestamp_sync_error(exc):
                raise
            logger.warning("Erro de timestamp na Binance em %s; recalibrando relogio da exchange e tentando novamente.", method_name)
            self._refresh_exchange_time_difference(exchange)
            return method(*args, **kwargs)

    def _build_authenticated_exchange(self, context: Dict[str, Any], *, testnet: Optional[bool] = None):
        exchange_name = str(context.get("exchange_name") or context.get("exchange") or "binanceusdm")
        if self._uses_env_credentials(context):
            credentials = self._load_env_credentials(context)
            exchange = ExchangeConfig.get_exchange_instance_with_credentials(
                exchange_name=exchange_name,
                api_key=credentials.get("api_key", ""),
                api_secret=credentials.get("api_secret", ""),
                testnet=self._resolve_testnet(testnet),
            )
            self._refresh_exchange_time_difference(exchange)
            return exchange, credentials

        if not self.is_ready():
            raise RuntimeError("CredentialVault nao configurado. Defina CREDENTIAL_ENCRYPTION_KEY antes da execucao real.")

        credentials = self.credential_vault.load_exchange_credentials(
            self.database,
            user_id=int(context["user_id"]),
            account_id=str(context["account_id"]),
            exchange=exchange_name,
        )
        exchange = ExchangeConfig.get_exchange_instance_with_credentials(
            exchange_name=exchange_name,
            api_key=credentials.get("api_key", ""),
            api_secret=credentials.get("api_secret", ""),
            testnet=self._resolve_testnet(testnet),
        )
        self._refresh_exchange_time_difference(exchange)
        return exchange, credentials

    @staticmethod
    def _resolve_exchange_symbol(exchange, symbol: str) -> str:
        markets = exchange.load_markets() or {}
        if symbol in markets:
            return symbol

        target = _compact_symbol(symbol)
        for market_symbol, market in markets.items():
            market_id = _compact_symbol(market.get("id") or market_symbol)
            compact_symbol = _compact_symbol(market_symbol)
            if target in {market_id, compact_symbol}:
                return market_symbol
            if compact_symbol.startswith(target) or market_id.startswith(target):
                return market_symbol
        raise ValueError(f"Simbolo {symbol} nao encontrado na exchange {getattr(exchange, 'id', 'unknown')}.")

    @staticmethod
    def _normalize_order_status(raw_status: Any) -> str:
        status = str(raw_status or "unknown").strip().lower()
        if not status:
            return "unknown"
        return status

    @staticmethod
    def _normalize_signal_to_order_side(signal_side: str) -> str:
        value = str(signal_side or "").strip().lower()
        if value in {"buy", "long", "compra"}:
            return "buy"
        if value in {"sell", "short", "venda"}:
            return "sell"
        raise ValueError(f"Lado de sinal invalido para execucao: {signal_side}")

    @staticmethod
    def _build_client_order_id(context: Dict[str, Any], symbol: str, side: str) -> str:
        compact = _compact_symbol(symbol).lower()[:10]
        account_token = str(context.get("account_id") or "acct").replace("-", "")[:6].lower()
        user_token = str(context.get("user_id") or "u")[:4]
        side_token = "b" if side == "buy" else "s"
        unique_token = uuid.uuid4().hex[:10]
        return f"evo{user_token}{account_token}{compact}{side_token}{unique_token}"[:36]

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value or default)
        except (TypeError, ValueError):
            return float(default)

    @staticmethod
    def _safe_float_or_none(value: Any) -> Optional[float]:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _weighted_trade_price(trades: List[Dict[str, Any]]) -> tuple[float, float]:
        total_qty = 0.0
        total_notional = 0.0
        for trade in trades or []:
            info = trade.get("info") or {}
            qty = LiveExecutionService._safe_float_or_none(
                trade.get("amount")
                or trade.get("filled")
                or trade.get("quantity")
                or info.get("qty")
                or info.get("executedQty")
            )
            price = LiveExecutionService._safe_float_or_none(
                trade.get("price")
                or trade.get("average")
                or info.get("price")
                or info.get("avgPrice")
            )
            if qty is None or qty <= 0 or price is None or price <= 0:
                continue
            total_qty += qty
            total_notional += qty * price
        if total_qty <= 0 or total_notional <= 0:
            return 0.0, 0.0
        return total_notional / total_qty, total_qty

    def _resolve_order_fill_details(
        self,
        exchange,
        *,
        resolved_symbol: str,
        order: Dict[str, Any],
        client_order_id: str,
    ) -> Dict[str, Any]:
        info = order.get("info") or {}
        order_id = str(order.get("id") or info.get("orderId") or "").strip()
        candidates: List[tuple[str, Dict[str, Any]]] = [("create_order", order)]

        if order_id and hasattr(exchange, "fetch_order"):
            try:
                fetched_order = self._call_signed_exchange(exchange, "fetch_order", order_id, resolved_symbol)
            except Exception:
                fetched_order = None
            if isinstance(fetched_order, dict):
                candidates.append(("fetch_order", fetched_order))

        for source_name, candidate in candidates:
            candidate_info = candidate.get("info") or {}
            fill_price = self._safe_float_or_none(
                candidate.get("average")
                or candidate.get("price")
                or candidate_info.get("avgPrice")
                or candidate_info.get("price")
            )
            fill_qty = self._safe_float_or_none(
                candidate.get("filled")
                or candidate.get("amount")
                or candidate_info.get("executedQty")
                or candidate_info.get("origQty")
            )
            if fill_price is not None and fill_price > 0:
                return {
                    "price": float(fill_price),
                    "quantity": float(fill_qty or 0.0),
                    "source": source_name,
                    "order_status": self._normalize_order_status(
                        candidate.get("status") or candidate_info.get("status")
                    ),
                }

        if hasattr(exchange, "fetch_my_trades"):
            try:
                recent_trades = self._call_signed_exchange(exchange, "fetch_my_trades", resolved_symbol, limit=25) or []
            except Exception:
                recent_trades = []
            matched_trades = []
            for trade in recent_trades:
                trade_info = trade.get("info") or {}
                trade_order_id = str(trade.get("order") or trade_info.get("orderId") or "").strip()
                trade_client_order_id = str(trade_info.get("clientOrderId") or "").strip()
                if order_id and trade_order_id == order_id:
                    matched_trades.append(trade)
                    continue
                if client_order_id and trade_client_order_id == client_order_id:
                    matched_trades.append(trade)
            weighted_price, weighted_qty = self._weighted_trade_price(matched_trades)
            if weighted_price > 0:
                return {
                    "price": float(weighted_price),
                    "quantity": float(weighted_qty),
                    "source": "fetch_my_trades",
                    "order_status": self._normalize_order_status(order.get("status") or info.get("status")),
                }

        return {
            "price": 0.0,
            "quantity": 0.0,
            "source": "unresolved",
            "order_status": self._normalize_order_status(order.get("status") or info.get("status")),
        }

    def _normalize_ccxt_order(
        self,
        order: Dict[str, Any],
        *,
        context: Dict[str, Any],
        symbol: str,
        timeframe: Optional[str],
        strategy_version: Optional[str],
        source: str,
    ) -> Dict[str, Any]:
        info = order.get("info") or {}
        return {
            "user_id": int(context["user_id"]),
            "account_id": str(context["account_id"]),
            "exchange": str(context.get("exchange_name") or context.get("exchange") or ""),
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_version": strategy_version,
            "client_order_id": order.get("clientOrderId") or info.get("clientOrderId"),
            "exchange_order_id": str(order.get("id") or info.get("orderId") or ""),
            "side": str(order.get("side") or info.get("side") or "").lower(),
            "order_type": str(order.get("type") or info.get("type") or "").lower(),
            "quantity": self._safe_float(order.get("amount") or info.get("origQty")),
            "price": self._safe_float(order.get("average") or order.get("price") or info.get("avgPrice") or info.get("price")),
            "status": self._normalize_order_status(order.get("status") or info.get("status")),
            "source": source,
            "notes": None,
        }

    def _normalize_ccxt_position(
        self,
        position: Dict[str, Any],
        *,
        context: Dict[str, Any],
        symbol: str,
        timeframe: Optional[str],
        strategy_version: Optional[str],
    ) -> Optional[Dict[str, Any]]:
        info = position.get("info") or {}
        signed_qty = self._safe_float_or_none(info.get("positionAmt"))
        raw_contracts = self._safe_float_or_none(position.get("contracts"))
        if signed_qty is not None:
            quantity = abs(float(signed_qty))
        elif raw_contracts is not None:
            quantity = abs(float(raw_contracts))
        else:
            quantity = abs(
                self._safe_float(
                    position.get("amount")
                    or position.get("size")
                    or position.get("positionAmt")
                    or 0.0
                )
            )
        if quantity <= 0:
            return None

        raw_side = str(position.get("side") or "").strip().lower()
        if raw_side not in {"long", "short"}:
            signed_qty_value = self._safe_float(info.get("positionAmt"))
            raw_side = "long" if signed_qty_value >= 0 else "short"

        return {
            "user_id": int(context["user_id"]),
            "account_id": str(context["account_id"]),
            "exchange": str(context.get("exchange_name") or context.get("exchange") or ""),
            "symbol": symbol,
            "timeframe": timeframe,
            "strategy_version": strategy_version,
            "side": raw_side,
            "quantity": quantity,
            "entry_price": self._safe_float(position.get("entryPrice") or info.get("entryPrice")),
            "mark_price": self._safe_float(position.get("markPrice") or info.get("markPrice") or position.get("lastPrice")),
            "unrealized_pnl": self._safe_float(
                position.get("unrealizedPnl") or info.get("unRealizedProfit") or info.get("unrealizedProfit")
            ),
            "status": "open",
            "notes": "reconciled_from_exchange",
        }

    def _save_execution_event(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        timeframe: Optional[str],
        strategy_version: Optional[str],
        event_type: str,
        event_status: str,
        message: str,
        details: Optional[Dict[str, Any]] = None,
        ) -> int:
        return self.database.save_user_execution_event(
            {
                "user_id": int(context["user_id"]),
                "account_id": str(context["account_id"]),
                "exchange": context.get("exchange_name") or context.get("exchange"),
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy_version": strategy_version,
                "event_type": event_type,
                "event_status": event_status,
                "message": message,
                "details_json": details or {},
            }
        )

    def fetch_account_balance_snapshot(
        self,
        context: Dict[str, Any],
        *,
        quote_asset: str = "USDT",
        testnet: Optional[bool] = None,
    ) -> Dict[str, Any]:
        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
        balance = self._call_signed_exchange(exchange, "fetch_balance")

        asset = str(quote_asset or "USDT").upper()
        asset_bucket = balance.get(asset) if isinstance(balance, dict) else {}
        free = self._safe_float((asset_bucket or {}).get("free"))
        used = self._safe_float((asset_bucket or {}).get("used"))
        total = self._safe_float((asset_bucket or {}).get("total"))

        if total <= 0 and isinstance(balance, dict):
            totals = balance.get("total")
            frees = balance.get("free")
            useds = balance.get("used")
            if isinstance(totals, dict):
                total = self._safe_float(totals.get(asset))
            if isinstance(frees, dict) and free <= 0:
                free = self._safe_float(frees.get(asset))
            if isinstance(useds, dict) and used <= 0:
                used = self._safe_float(useds.get(asset))

        info = balance.get("info") if isinstance(balance, dict) else None
        if isinstance(info, dict):
            for item in info.get("assets") or []:
                if str(item.get("asset") or "").upper() != asset:
                    continue
                if total <= 0:
                    total = self._safe_float(item.get("walletBalance") or item.get("marginBalance"))
                if free <= 0:
                    free = self._safe_float(item.get("availableBalance") or item.get("maxWithdrawAmount"))
                break

        if total <= 0:
            total = free

        return {
            "ok": True,
            "quote_asset": asset,
            "total": round(total, 8),
            "free": round(free, 8),
            "used": round(used, 8),
            "environment": "testnet" if self._resolve_testnet(testnet) else "mainnet",
        }

    @classmethod
    def _extract_market_rules(cls, market: Dict[str, Any]) -> Dict[str, float]:
        def _positive_min(*values: float) -> float:
            positives = [float(value) for value in values if float(value or 0.0) > 0.0]
            return min(positives) if positives else 0.0

        market = market or {}
        limits = market.get("limits") or {}
        amount_limits = limits.get("amount") or {}
        cost_limits = limits.get("cost") or {}
        price_limits = limits.get("price") or {}
        precision = market.get("precision") or {}
        info = market.get("info") or {}
        filters = info.get("filters") or []

        min_qty = 0.0
        min_notional = 0.0
        step_size = 0.0
        min_price_tick = 0.0

        try:
            min_qty = float(amount_limits.get("min") or 0.0)
        except (TypeError, ValueError):
            min_qty = 0.0
        try:
            min_notional = float(cost_limits.get("min") or 0.0)
        except (TypeError, ValueError):
            min_notional = 0.0
        try:
            min_price_tick = float(price_limits.get("min") or 0.0)
        except (TypeError, ValueError):
            min_price_tick = 0.0

        amount_precision = precision.get("amount")
        if amount_precision is not None:
            try:
                step_size = 10 ** (-int(amount_precision))
            except (TypeError, ValueError, OverflowError):
                step_size = 0.0

        for raw_filter in filters:
            if not isinstance(raw_filter, dict):
                continue
            filter_type = str(raw_filter.get("filterType") or "").upper()
            if filter_type == "LOT_SIZE":
                try:
                    min_qty = max(min_qty, float(raw_filter.get("minQty") or 0.0))
                except (TypeError, ValueError):
                    pass
                try:
                    step_size = _positive_min(step_size, float(raw_filter.get("stepSize") or 0.0))
                except (TypeError, ValueError):
                    pass
            elif filter_type in {"MIN_NOTIONAL", "NOTIONAL"}:
                try:
                    min_notional = max(
                        min_notional,
                        float(raw_filter.get("minNotional") or raw_filter.get("notional") or 0.0),
                    )
                except (TypeError, ValueError):
                    pass
            elif filter_type == "PRICE_FILTER":
                try:
                    min_price_tick = _positive_min(min_price_tick, float(raw_filter.get("tickSize") or 0.0))
                except (TypeError, ValueError):
                    pass

        return {
            "min_qty": round(min_qty, 12),
            "min_notional": round(min_notional, 8),
            "qty_step": round(step_size, 12),
            "min_price_tick": round(min_price_tick, 12),
            "contract_size": cls._safe_float(market.get("contractSize"), 0.0),
        }

    def fetch_symbol_trading_rules(
        self,
        context: Dict[str, Any],
        *,
        symbol: str,
        testnet: Optional[bool] = None,
    ) -> Dict[str, Any]:
        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
        markets = exchange.load_markets() or {}
        resolved_symbol = self._resolve_exchange_symbol(exchange, symbol)
        market = markets.get(resolved_symbol) or {}
        rules = self._extract_market_rules(market)
        rules.update(
            {
                "symbol": symbol,
                "exchange_symbol": resolved_symbol,
                "exchange_id": str(getattr(exchange, "id", "unknown") or "unknown"),
            }
        )
        return rules

    def validate_account_connection(self, context: Dict[str, Any], *, testnet: Optional[bool] = None) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        exchange_name = str(context.get("exchange_name") or context.get("exchange") or "")
        try:
            exchange, credentials = self._build_authenticated_exchange(context, testnet=testnet)
            exchange.load_markets()
            try:
                self._call_signed_exchange(exchange, "fetch_balance")
                permission_status = "valid"
            except Exception as balance_exc:
                logger.warning("Falha ao validar balance da conta %s/%s: %s", context.get("user_id"), context.get("account_id"), balance_exc)
                error_message = str(balance_exc)
                self.database.update_user_exchange_credential_status(
                    user_id=int(context["user_id"]),
                    account_id=str(context["account_id"]),
                    exchange=exchange_name,
                    permission_status="invalid",
                    token_status="invalid",
                    last_validated_at=now_iso,
                    notes=error_message,
                )
                self._save_execution_event(
                    context=context,
                    symbol="*",
                    timeframe="*",
                    strategy_version="validation",
                    event_type="credential_validation",
                    event_status="error",
                    message=f"Falha ao validar balance da conta: {balance_exc}",
                    details={"environment": "testnet" if self._resolve_testnet(testnet) else "mainnet"},
                )
                return {
                    "ok": False,
                    "error": error_message,
                    "permission_status": "invalid",
                    "token_status": "invalid",
                    "environment": "testnet" if self._resolve_testnet(testnet) else "mainnet",
                }
            token_status = "valid"
            self.database.update_user_exchange_credential_status(
                user_id=int(context["user_id"]),
                account_id=str(context["account_id"]),
                exchange=exchange_name,
                permission_status=permission_status,
                token_status=token_status,
                last_validated_at=now_iso,
            )
            self._save_execution_event(
                context=context,
                symbol="*",
                timeframe="*",
                strategy_version="validation",
                event_type="credential_validation",
                event_status="ok",
                message="Credenciais validadas com sucesso.",
                details={
                    "environment": "testnet" if self._resolve_testnet(testnet) else "mainnet",
                    "exchange": getattr(exchange, "id", exchange_name),
                    "credential_alias": credentials.get("credential_alias"),
                },
            )
            return {
                "ok": True,
                "permission_status": permission_status,
                "token_status": token_status,
                "environment": "testnet" if self._resolve_testnet(testnet) else "mainnet",
            }
        except Exception as exc:
            self.database.update_user_exchange_credential_status(
                user_id=int(context["user_id"]),
                account_id=str(context["account_id"]),
                exchange=exchange_name,
                permission_status="invalid",
                token_status="invalid",
                last_validated_at=now_iso,
                notes=str(exc),
            )
            self._save_execution_event(
                context=context,
                symbol="*",
                timeframe="*",
                strategy_version="validation",
                event_type="credential_validation",
                event_status="error",
                message=f"Falha na validacao de credenciais: {exc}",
                details={"environment": "testnet" if self._resolve_testnet(testnet) else "mainnet"},
            )
            return {
                "ok": False,
                "error": str(exc),
                "permission_status": "invalid",
                "token_status": "invalid",
            }

    def submit_market_order(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        timeframe: str,
        strategy_version: Optional[str],
        signal_side: str,
        quantity: float,
        reduce_only: bool = False,
        source: str = "live_execution",
        testnet: Optional[bool] = None,
        leverage: Optional[int] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_side = self._normalize_signal_to_order_side(signal_side)
        resolved_quantity = self._safe_float(quantity)
        if resolved_quantity <= 0:
            raise ValueError("Quantidade invalida para ordem real.")

        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
        resolved_symbol = self._resolve_exchange_symbol(exchange, symbol)
        client_order_id = self._build_client_order_id(context, symbol, resolved_side)

        if leverage:
            try:
                self._call_signed_exchange(exchange, "set_leverage", int(leverage), resolved_symbol)
            except Exception as leverage_exc:
                logger.warning("Falha ao ajustar leverage para %s: %s", resolved_symbol, leverage_exc)

        try:
            precise_quantity = resolved_quantity
            if hasattr(exchange, "amount_to_precision"):
                precise_quantity = float(exchange.amount_to_precision(resolved_symbol, resolved_quantity))
            if precise_quantity <= 0:
                raise ValueError(
                    "Quantidade invalida apos arredondamento da exchange "
                    f"({resolved_quantity} -> {precise_quantity})."
                )
            params = self._exchange_request_params(
                {"clientOrderId": client_order_id},
                exchange_name=str(context.get("exchange_name") or context.get("exchange") or "binanceusdm"),
            )
            if reduce_only:
                params["reduceOnly"] = True
            order = self._call_signed_exchange(
                exchange,
                "create_order",
                resolved_symbol,
                "market",
                resolved_side,
                precise_quantity,
                None,
                params,
            )
            fill_details = self._resolve_order_fill_details(
                exchange,
                resolved_symbol=resolved_symbol,
                order=order,
                client_order_id=client_order_id,
            )
            normalized_order = self._normalize_ccxt_order(
                order,
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                source=source,
            )
            if float(fill_details.get("price") or 0.0) > 0:
                normalized_order["price"] = float(fill_details["price"])
            if float(fill_details.get("quantity") or 0.0) > 0:
                normalized_order["quantity"] = float(fill_details["quantity"])
            normalized_order["status"] = str(fill_details.get("order_status") or normalized_order.get("status") or "unknown")
            order_id = self.database.upsert_user_live_order(normalized_order)
            event_id = self._save_execution_event(
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                event_type="order_submitted",
                event_status="ok",
                message=f"Ordem {resolved_side} enviada com sucesso.",
                details={
                    "reduce_only": bool(reduce_only),
                    "client_order_id": client_order_id,
                    "exchange_order_id": normalized_order.get("exchange_order_id"),
                    "quantity": precise_quantity,
                    "fill_price": normalized_order.get("price"),
                    "fill_price_source": fill_details.get("source"),
                    "source": source,
                    "metadata": metadata or {},
                },
            )
            reconciliation = self.reconcile_account_state(
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                testnet=testnet,
                source=f"{source}_post_order",
            )
            fill_price = float(normalized_order.get("price") or 0.0)
            fill_price_source = str(fill_details.get("source") or "unresolved")
            if fill_price <= 0 and not bool(reduce_only):
                try:
                    live_positions = self.database.get_user_live_positions(
                        user_id=int(context["user_id"]),
                        account_id=str(context["account_id"]),
                        status="open",
                    )
                except Exception:
                    live_positions = []
                target_positions = [
                    row
                    for row in (live_positions or [])
                    if str(row.get("symbol") or "").strip().upper() == str(symbol or "").strip().upper()
                ]
                if len(target_positions) == 1:
                    fill_price = self._safe_float(target_positions[0].get("entry_price"))
                    fill_price_source = "reconciliation_position"
            return {
                "ok": True,
                "order_id": order_id,
                "event_id": event_id,
                "client_order_id": client_order_id,
                "exchange_order_id": normalized_order.get("exchange_order_id"),
                "order_status": normalized_order.get("status"),
                "price": fill_price,
                "fill_price_source": fill_price_source,
                "quantity": float(normalized_order.get("quantity") or precise_quantity),
                "reconciliation": reconciliation,
            }
        except Exception as exc:
            self._save_execution_event(
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                event_type="order_submit_error",
                event_status="error",
                message=f"Falha ao enviar ordem real: {exc}",
                details={
                    "reduce_only": bool(reduce_only),
                    "client_order_id": client_order_id,
                    "quantity": resolved_quantity,
                    "source": source,
                    "metadata": metadata or {},
                },
            )
            raise

    def _submit_conditional_market_order(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        signal_side: str,
        quantity: float,
        stop_price: float,
        order_type: str,
        source: str,
        testnet: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resolved_side = self._normalize_signal_to_order_side(signal_side)
        resolved_quantity = self._safe_float(quantity)
        resolved_stop_price = self._safe_float(stop_price)
        if resolved_quantity <= 0:
            raise ValueError("Quantidade invalida para ordem condicional.")
        if resolved_stop_price <= 0:
            raise ValueError("stop_price invalido para ordem condicional.")

        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
        resolved_symbol = self._resolve_exchange_symbol(exchange, symbol)
        client_order_id = self._build_client_order_id(context, symbol, resolved_side)

        precise_quantity = resolved_quantity
        precise_stop_price = resolved_stop_price
        if hasattr(exchange, "amount_to_precision"):
            precise_quantity = float(exchange.amount_to_precision(resolved_symbol, resolved_quantity))
        if hasattr(exchange, "price_to_precision"):
            precise_stop_price = float(exchange.price_to_precision(resolved_symbol, resolved_stop_price))

        exchange_name = str(context.get("exchange_name") or context.get("exchange") or "binanceusdm")
        params = self._exchange_request_params(
            {
                "clientOrderId": client_order_id,
                "stopPrice": precise_stop_price,
                "reduceOnly": True,
                "workingType": "MARK_PRICE",
            },
            exchange_name=exchange_name,
        )
        order = self._call_signed_exchange(
            exchange,
            "create_order",
            resolved_symbol,
            order_type,
            resolved_side,
            precise_quantity,
            None,
            params,
        )
        normalized_order = self._normalize_ccxt_order(
            order,
            context=context,
            symbol=symbol,
            timeframe=None,
            strategy_version=None,
            source=source,
        )
        order_id = self.database.upsert_user_live_order(normalized_order)
        event_id = self._save_execution_event(
            context=context,
            symbol=symbol,
            timeframe="*",
            strategy_version="conditional_order",
            event_type="conditional_order_submitted",
            event_status="ok",
            message=f"Ordem condicional {order_type} enviada com sucesso.",
            details={
                "client_order_id": client_order_id,
                "exchange_order_id": normalized_order.get("exchange_order_id"),
                "quantity": precise_quantity,
                "stop_price": precise_stop_price,
                "source": source,
                "metadata": metadata or {},
            },
        )
        return {
            "ok": True,
            "order_id": order_id,
            "event_id": event_id,
            "client_order_id": client_order_id,
            "exchange_order_id": normalized_order.get("exchange_order_id"),
            "order_status": normalized_order.get("status"),
            "price": normalized_order.get("price"),
            "quantity": precise_quantity,
            "stop_price": precise_stop_price,
        }

    def submit_stop_market_order(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        side: str,
        stop_price: float,
        quantity: float,
        testnet: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        exchange_name = ExchangeConfig.normalize_exchange_name(
            str(context.get("exchange_name") or context.get("exchange") or "binanceusdm")
        )
        order_type = "stopMarket" if exchange_name == "bybit" else "STOP_MARKET"
        return self._submit_conditional_market_order(
            context=context,
            symbol=symbol,
            signal_side=side,
            quantity=quantity,
            stop_price=stop_price,
            order_type=order_type,
            source="live_execution_stop_market",
            testnet=testnet,
            metadata=metadata,
        )

    def submit_take_profit_market_order(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        side: str,
        stop_price: float,
        quantity: float,
        testnet: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return self._submit_conditional_market_order(
            context=context,
            symbol=symbol,
            signal_side=side,
            quantity=quantity,
            stop_price=stop_price,
            order_type="TAKE_PROFIT_MARKET",
            source="live_execution_take_profit_market",
            testnet=testnet,
            metadata=metadata,
        )

    def cancel_order(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        order_id: str,
        testnet: Optional[bool] = None,
    ) -> Dict[str, Any]:
        resolved_order_id = str(order_id or "").strip()
        if not resolved_order_id:
            raise ValueError("order_id obrigatorio para cancelamento.")

        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
        resolved_symbol = self._resolve_exchange_symbol(exchange, symbol)
        self._call_signed_exchange(
            exchange,
            "cancel_order",
            resolved_order_id,
            resolved_symbol,
            params=self._exchange_request_params(
                exchange_name=str(context.get("exchange_name") or context.get("exchange") or "binanceusdm")
            ),
        )
        event_id = self._save_execution_event(
            context=context,
            symbol=symbol,
            timeframe="*",
            strategy_version="cancel_order",
            event_type="cancel_order",
            event_status="ok",
            message=f"Ordem {resolved_order_id} cancelada com sucesso.",
            details={"exchange_order_id": resolved_order_id},
        )
        return {"ok": True, "event_id": event_id, "exchange_order_id": resolved_order_id}

    @staticmethod
    def _is_unknown_order_error(exc: Exception) -> bool:
        message = str(exc or "").lower()
        return "unknown order" in message or "-2011" in message

    @staticmethod
    def _is_unknown_order_text(message: str) -> bool:
        normalized = str(message or "").lower()
        return "unknown order" in normalized or "-2011" in normalized

    @staticmethod
    def _order_matches_stop_market(order: Dict[str, Any], *, side: Optional[str] = None) -> bool:
        info = order.get("info") or {}
        raw_type = str(order.get("type") or info.get("type") or "").replace("_", "").lower()
        if raw_type not in {"stopmarket", "stop_market"} and "stopmarket" not in raw_type:
            return False
        if side:
            order_side = str(order.get("side") or info.get("side") or "").strip().lower()
            if order_side and order_side != str(side).strip().lower():
                return False
        return True

    def cancel_open_stop_market_orders(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        side: Optional[str] = None,
        testnet: Optional[bool] = None,
    ) -> Dict[str, Any]:
        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
        resolved_symbol = self._resolve_exchange_symbol(exchange, symbol)
        if not hasattr(exchange, "fetch_open_orders"):
            return {"ok": True, "cancelled": 0, "skipped": "fetch_open_orders_unavailable"}

        open_orders = self._call_signed_exchange(exchange, "fetch_open_orders", resolved_symbol) or []
        cancelled = 0
        cancelled_ids: List[str] = []
        for order in open_orders:
            if not self._order_matches_stop_market(order, side=side):
                continue
            order_id = str(order.get("id") or (order.get("info") or {}).get("orderId") or "").strip()
            if not order_id:
                continue
            self._call_signed_exchange(
                exchange,
                "cancel_order",
                order_id,
                resolved_symbol,
                params=self._exchange_request_params(
                    exchange_name=str(context.get("exchange_name") or context.get("exchange") or "binanceusdm")
                ),
            )
            cancelled += 1
            cancelled_ids.append(order_id)

        if cancelled:
            self._save_execution_event(
                context=context,
                symbol=symbol,
                timeframe="*",
                strategy_version="cancel_open_stop_market_orders",
                event_type="cancel_open_stop_market_orders",
                event_status="ok",
                message=f"Stops STOP_MARKET pendentes cancelados antes da substituicao ({cancelled}).",
                details={"cancelled": cancelled, "order_ids": cancelled_ids, "side": side},
            )
        return {"ok": True, "cancelled": cancelled, "order_ids": cancelled_ids}

    def replace_stop_market_order(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        side: str,
        stop_price: float,
        quantity: float,
        previous_order_id: Optional[str] = None,
        testnet: Optional[bool] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        cancel_result = None
        cancel_error = None
        resolved_previous_order_id = str(previous_order_id or "").strip()
        if resolved_previous_order_id:
            try:
                cancel_result = self.cancel_order(
                    context=context,
                    symbol=symbol,
                    order_id=resolved_previous_order_id,
                    testnet=testnet,
                )
            except Exception as exc:
                cancel_error = str(exc)
                if not self._is_unknown_order_error(exc):
                    self._save_execution_event(
                        context=context,
                        symbol=symbol,
                        timeframe="*",
                        strategy_version="replace_stop_market_order",
                        event_type="cancel_previous_stop_error",
                        event_status="error",
                        message=f"Substituicao abortada: falha ao cancelar stop anterior {resolved_previous_order_id}: {exc}",
                        details={
                            "previous_order_id": resolved_previous_order_id,
                            "stop_price": float(stop_price or 0.0),
                        },
                    )
                    raise RuntimeError(
                        f"Substituicao de stop abortada; stop anterior nao foi cancelado: {exc}"
                    ) from exc
                self._save_execution_event(
                    context=context,
                    symbol=symbol,
                    timeframe="*",
                    strategy_version="replace_stop_market_order",
                    event_type="cancel_previous_stop_error",
                    event_status="warning",
                    message=f"Falha ao cancelar stop anterior {resolved_previous_order_id}: {exc}",
                    details={
                        "previous_order_id": resolved_previous_order_id,
                        "stop_price": float(stop_price or 0.0),
                    },
                )

        sweep_result = self.cancel_open_stop_market_orders(
            context=context,
            symbol=symbol,
            side=side,
            testnet=testnet,
        )
        if resolved_previous_order_id and cancel_error and self._is_unknown_order_text(cancel_error):
            raise RuntimeError(
                "Substituicao de stop bloqueada: a exchange nao confirmou cancelamento "
                f"do stop anterior {resolved_previous_order_id}. Nenhum novo stop foi enviado."
            )
        new_order = self.submit_stop_market_order(
            context=context,
            symbol=symbol,
            side=side,
            stop_price=stop_price,
            quantity=quantity,
            testnet=testnet,
            metadata=metadata,
        )
        return {
            **new_order,
            "previous_order_id": resolved_previous_order_id or None,
            "previous_cancelled": bool(cancel_result),
            "previous_cancel_error": cancel_error,
            "stale_stops_cancelled": int(sweep_result.get("cancelled", 0) or 0),
        }

    def cancel_all_symbol_orders(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        testnet: Optional[bool] = None,
    ) -> Dict[str, Any]:
        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
        resolved_symbol = self._resolve_exchange_symbol(exchange, symbol)
        open_orders = (
            self._call_signed_exchange(exchange, "fetch_open_orders", resolved_symbol)
            if hasattr(exchange, "fetch_open_orders")
            else []
        )

        cancelled = 0
        for order in open_orders or []:
            order_id = order.get("id") or (order.get("info") or {}).get("orderId")
            if not order_id:
                continue
            self._call_signed_exchange(
                exchange,
                "cancel_order",
                order_id,
                resolved_symbol,
                params=self._exchange_request_params(
                    exchange_name=str(context.get("exchange_name") or context.get("exchange") or "binanceusdm")
                ),
            )
            cancelled += 1

        self._save_execution_event(
            context=context,
            symbol=symbol,
            timeframe="*",
            strategy_version="cancel_orders",
            event_type="cancel_symbol_orders",
            event_status="ok",
            message=f"Cancelamento de ordens pendentes concluido ({cancelled}).",
            details={"cancelled": cancelled},
        )
        return {"ok": True, "cancelled": cancelled}

    def reconcile_account_state(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        timeframe: Optional[str],
        strategy_version: Optional[str],
        testnet: Optional[bool] = None,
        source: str = "live_reconciliation",
    ) -> Dict[str, Any]:
        exchange_name = str(context.get("exchange_name") or context.get("exchange") or "")
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)
            resolved_symbol = self._resolve_exchange_symbol(exchange, symbol)

            raw_open_orders = (
                self._call_signed_exchange(exchange, "fetch_open_orders", resolved_symbol)
                if hasattr(exchange, "fetch_open_orders")
                else []
            )
            normalized_orders = [
                self._normalize_ccxt_order(
                    item,
                    context=context,
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_version=strategy_version,
                    source=source,
                )
                for item in (raw_open_orders or [])
            ]
            order_ids = self.database.sync_user_live_orders_snapshot(
                user_id=int(context["user_id"]),
                account_id=str(context["account_id"]),
                exchange=exchange_name,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                open_orders=normalized_orders,
                absent_status="closed_on_exchange",
            )

            raw_positions: List[Dict[str, Any]] = []
            if getattr(exchange, "has", {}).get("fetchPositions"):
                raw_positions = self._call_signed_exchange(exchange, "fetch_positions", [resolved_symbol])
            normalized_positions = []
            for position in raw_positions or []:
                normalized = self._normalize_ccxt_position(
                    position,
                    context=context,
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_version=strategy_version,
                )
                if normalized:
                    normalized_positions.append(normalized)

            position_ids = self.database.sync_user_live_positions_snapshot(
                user_id=int(context["user_id"]),
                account_id=str(context["account_id"]),
                exchange=exchange_name,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                positions=normalized_positions,
            )
            self.database.update_user_exchange_credential_status(
                user_id=int(context["user_id"]),
                account_id=str(context["account_id"]),
                exchange=exchange_name,
                reconciliation_status="ok",
                last_validated_at=now_iso,
            )
            event_id = self._save_execution_event(
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                event_type="exchange_reconciled",
                event_status="ok",
                message="Reconciliação com a exchange concluída.",
                details={
                    "orders_open": len(normalized_orders),
                    "positions_open": len(normalized_positions),
                    "source": source,
                },
            )
            return {
                "ok": True,
                "event_id": event_id,
                "order_ids": order_ids,
                "position_ids": position_ids,
                "orders_open": len(normalized_orders),
                "positions_open": len(normalized_positions),
            }
        except Exception as exc:
            self.database.update_user_exchange_credential_status(
                user_id=int(context["user_id"]),
                account_id=str(context["account_id"]),
                exchange=exchange_name,
                reconciliation_status="broken",
                last_validated_at=now_iso,
                notes=str(exc),
            )
            self._save_execution_event(
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                event_type="exchange_reconciled",
                event_status="error",
                message=f"Falha na reconciliação com a exchange: {exc}",
                details={"source": source},
            )
            return {"ok": False, "error": str(exc), "orders_open": 0, "positions_open": 0}

    def start_user_data_stream(
        self,
        *,
        context: Dict[str, Any],
        symbol: str,
        timeframe: str,
        strategy_version: Optional[str],
        testnet: Optional[bool] = None,
    ) -> BinanceFuturesUserDataStream:
        exchange, _ = self._build_authenticated_exchange(context, testnet=testnet)

        def _on_event(payload: Dict[str, Any]) -> None:
            event_type = str(payload.get("e") or "user_stream_event")
            self._save_execution_event(
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                event_type=event_type,
                event_status="stream_event",
                message=f"Evento recebido do user data stream: {event_type}",
                details={"payload": payload},
            )
            if event_type in {"ORDER_TRADE_UPDATE", "ACCOUNT_UPDATE"}:
                self.reconcile_account_state(
                    context=context,
                    symbol=symbol,
                    timeframe=timeframe,
                    strategy_version=strategy_version,
                    testnet=testnet,
                    source="user_stream",
                )

        stream = BinanceFuturesUserDataStream(
            exchange,
            testnet=self._resolve_testnet(testnet),
            on_event=_on_event,
        )
        stream.start()
        return stream
