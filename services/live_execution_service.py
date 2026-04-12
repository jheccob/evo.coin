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

    def __init__(self, database=None, credential_vault: Optional[CredentialVault] = None):
        self.database = database or runtime_db
        self.credential_vault = credential_vault or CredentialVault(strict=False)

    def is_ready(self) -> bool:
        return bool(self.credential_vault and self.credential_vault.is_configured())

    def _resolve_testnet(self, testnet: Optional[bool] = None) -> bool:
        return bool(config.TESTNET if testnet is None else testnet)

    @staticmethod
    def _uses_env_credentials(context: Dict[str, Any]) -> bool:
        source = str(context.get("credential_source") or "").strip().lower()
        return bool(context.get("use_env_credentials")) or source == "env"

    @staticmethod
    def _load_env_credentials(context: Dict[str, Any]) -> Dict[str, str]:
        api_key = str(context.get("api_key") or os.getenv("BINANCE_API_KEY", "")).strip()
        api_secret = str(context.get("api_secret") or os.getenv("BINANCE_SECRET_KEY", "")).strip()
        if not api_key or not api_secret:
            raise RuntimeError(
                "Execucao live do runner exige BINANCE_API_KEY e BINANCE_SECRET_KEY configurados."
            )
        return {
            "user_id": int(context.get("user_id", 0) or 0),
            "account_id": str(context.get("account_id") or "env-primary"),
            "exchange": str(context.get("exchange_name") or context.get("exchange") or "binanceusdm"),
            "api_key_ref": "env_api_key",
            "token_ref": "env_secret_key",
            "credential_alias": str(context.get("account_alias") or context.get("account_id") or "env-primary"),
            "credential_source": "env",
            "api_key": api_key,
            "api_secret": api_secret,
        }

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
        raw_qty = (
            position.get("contracts")
            or position.get("contractSize")
            or info.get("positionAmt")
            or position.get("contracts")
            or 0.0
        )
        quantity = abs(self._safe_float(raw_qty))
        if quantity <= 0:
            return None

        raw_side = str(position.get("side") or "").strip().lower()
        if raw_side not in {"long", "short"}:
            signed_qty = self._safe_float(info.get("positionAmt"))
            raw_side = "long" if signed_qty >= 0 else "short"

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
        balance = exchange.fetch_balance()

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

    def validate_account_connection(self, context: Dict[str, Any], *, testnet: Optional[bool] = None) -> Dict[str, Any]:
        now_iso = datetime.now(timezone.utc).isoformat()
        exchange_name = str(context.get("exchange_name") or context.get("exchange") or "")
        try:
            exchange, credentials = self._build_authenticated_exchange(context, testnet=testnet)
            exchange.load_markets()
            try:
                exchange.fetch_balance()
                permission_status = "valid"
            except Exception as balance_exc:
                permission_status = "unknown"
                logger.warning("Falha ao validar balance da conta %s/%s: %s", context.get("user_id"), context.get("account_id"), balance_exc)
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
                exchange.set_leverage(int(leverage), resolved_symbol)
            except Exception as leverage_exc:
                logger.warning("Falha ao ajustar leverage para %s: %s", resolved_symbol, leverage_exc)

        try:
            precise_quantity = resolved_quantity
            if hasattr(exchange, "amount_to_precision"):
                precise_quantity = float(exchange.amount_to_precision(resolved_symbol, resolved_quantity))
            params = {"newClientOrderId": client_order_id}
            if reduce_only:
                params["reduceOnly"] = True
            order = exchange.create_order(
                resolved_symbol,
                "market",
                resolved_side,
                precise_quantity,
                None,
                params,
            )
            normalized_order = self._normalize_ccxt_order(
                order,
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                source=source,
            )
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
            return {
                "ok": True,
                "order_id": order_id,
                "event_id": event_id,
                "client_order_id": client_order_id,
                "exchange_order_id": normalized_order.get("exchange_order_id"),
                "order_status": normalized_order.get("status"),
                "quantity": precise_quantity,
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

            raw_open_orders = exchange.fetch_open_orders(resolved_symbol) if hasattr(exchange, "fetch_open_orders") else []
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
                raw_positions = exchange.fetch_positions([resolved_symbol])
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
