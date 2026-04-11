import logging
from datetime import datetime, timezone
from typing import Dict, List, Optional

from config import ProductionConfig
from database.database import db as runtime_db
from services.risk_management_service import RiskManagementService

logger = logging.getLogger(__name__)


class MultiUserRuntimeService:
    """
    Runtime multiuser com isolamento por conta.
    Esta fase nao executa ordens automaticas quando
    ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION estiver desabilitado.
    """

    def __init__(self, database=None, risk_management_service=None):
        self.database = database or runtime_db
        self.risk_management_service = risk_management_service or RiskManagementService(database=self.database)

    def run_cycle(
        self,
        symbol: str,
        timeframe: str,
        strategy_version: Optional[str] = None,
        entry_price: Optional[float] = None,
        stop_loss_pct: float = ProductionConfig.DEFAULT_LIVE_STOP_LOSS_PCT,
    ) -> List[Dict]:
        if not ProductionConfig.ENABLE_MULTIUSER_RUNTIME:
            return []

        contexts = self.database.list_eligible_accounts_for_runtime(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )

        results = []
        for context in contexts:
            result = self.run_account_cycle(
                context=context,
                symbol=symbol,
                timeframe=timeframe,
                strategy_version=strategy_version,
                entry_price=entry_price,
                stop_loss_pct=stop_loss_pct,
            )
            results.append(result)
        return results

    def run_account_cycle(
        self,
        *,
        context: Dict,
        symbol: str,
        timeframe: str,
        strategy_version: Optional[str],
        entry_price: Optional[float],
        stop_loss_pct: float,
    ) -> Dict:
        now_iso = datetime.now(timezone.utc).isoformat()
        user_id = int(context["user_id"])
        account_id = str(context["account_id"])
        exchange = str(context.get("exchange_name") or context.get("exchange") or "")
        account_alias = context.get("account_alias") or account_id

        hard_block_reason = self._evaluate_account_hard_block(context, symbol=symbol, timeframe=timeframe)
        if hard_block_reason:
            event = self.database.save_user_execution_event(
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "strategy_version": strategy_version,
                    "event_type": "account_blocked",
                    "event_status": "blocked",
                    "message": hard_block_reason,
                    "details_json": {
                        "account_alias": account_alias,
                        "source": "multiuser_runtime",
                        "timestamp": now_iso,
                    },
                }
            )
            return {
                "user_id": user_id,
                "account_id": account_id,
                "status": "blocked",
                "reason": hard_block_reason,
                "event_id": event,
            }

        resolved_entry = float(entry_price or 0.0)
        if resolved_entry <= 0:
            self.database.save_user_execution_event(
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "strategy_version": strategy_version,
                    "event_type": "runtime_error",
                    "event_status": "error",
                    "message": "Preco de entrada invalido para avaliacao de risco.",
                    "details_json": {"source": "multiuser_runtime", "timestamp": now_iso},
                }
            )
            return {
                "user_id": user_id,
                "account_id": account_id,
                "status": "error",
                "reason": "invalid_entry_price",
            }

        risk_profile = context.get("risk_profile") or {}
        risk_plan = self.risk_management_service.evaluate_risk_engine(
            entry_price=resolved_entry,
            stop_loss_pct=stop_loss_pct,
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            account_balance=float(context.get("capital_base") or ProductionConfig.PAPER_ACCOUNT_BALANCE),
            risk_per_trade_pct=float(risk_profile.get("max_risk_per_trade") or ProductionConfig.RISK_PER_TRADE_PCT),
            max_open_trades=int(risk_profile.get("allowed_position_count") or ProductionConfig.MAX_OPEN_PAPER_TRADES),
            max_open_trades_per_symbol=ProductionConfig.MAX_OPEN_PAPER_TRADES_PER_SYMBOL,
            max_portfolio_open_risk_pct=float(risk_profile.get("max_portfolio_open_risk_pct") or ProductionConfig.MAX_PORTFOLIO_OPEN_RISK_PCT),
            runtime_allowed=True,
        )

        if not risk_plan.get("allowed", False):
            event = self.database.save_user_execution_event(
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "strategy_version": strategy_version,
                    "event_type": "risk_blocked",
                    "event_status": "blocked",
                    "message": risk_plan.get("risk_reason") or risk_plan.get("reason") or "Risco bloqueado.",
                    "details_json": {
                        "source": "multiuser_runtime",
                        "risk_mode": risk_plan.get("risk_mode"),
                        "risk_status": risk_plan.get("risk_status"),
                        "timestamp": now_iso,
                    },
                }
            )
            return {
                "user_id": user_id,
                "account_id": account_id,
                "status": "blocked",
                "reason": risk_plan.get("reason"),
                "event_id": event,
            }

        if not ProductionConfig.ENABLE_MULTIUSER_AUTO_ORDER_EXECUTION:
            event = self.database.save_user_execution_event(
                {
                    "user_id": user_id,
                    "account_id": account_id,
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "strategy_version": strategy_version,
                    "event_type": "execution_skipped",
                    "event_status": "ready_no_auto_order",
                    "message": "Conta elegivel, mas autoexecucao de ordens esta desabilitada.",
                    "details_json": {
                        "source": "multiuser_runtime",
                        "auto_order_execution": False,
                        "risk_mode": risk_plan.get("risk_mode"),
                        "risk_amount": risk_plan.get("risk_amount"),
                        "position_notional": risk_plan.get("position_notional"),
                        "timestamp": now_iso,
                    },
                }
            )
            return {
                "user_id": user_id,
                "account_id": account_id,
                "status": "ready_no_auto_order",
                "event_id": event,
                "risk_plan": {
                    "risk_mode": risk_plan.get("risk_mode"),
                    "risk_amount": risk_plan.get("risk_amount"),
                    "position_notional": risk_plan.get("position_notional"),
                    "quantity": risk_plan.get("quantity"),
                },
            }

        event = self.database.save_user_execution_event(
            {
                "user_id": user_id,
                "account_id": account_id,
                "exchange": exchange,
                "symbol": symbol,
                "timeframe": timeframe,
                "strategy_version": strategy_version,
                "event_type": "execution_blocked_policy",
                "event_status": "blocked",
                "message": "Execucao automatica nao implementada nesta fase incremental.",
                "details_json": {"source": "multiuser_runtime", "timestamp": now_iso},
            }
        )
        return {
            "user_id": user_id,
            "account_id": account_id,
            "status": "blocked",
            "reason": "auto_execution_not_implemented",
            "event_id": event,
        }

    def _evaluate_account_hard_block(self, context: Dict, symbol: Optional[str] = None, timeframe: Optional[str] = None) -> Optional[str]:
        if not bool(context.get("live_enabled")):
            return "Conta sem live_enabled explicito."

        risk_profile = context.get("risk_profile") or {}
        if not risk_profile or not bool(risk_profile.get("is_valid", True)):
            return "Conta sem risk profile valido."

        token_status = str(context.get("token_status") or "unknown").lower()
        if ProductionConfig.REQUIRE_MULTIUSER_VALID_TOKEN and token_status not in {"valid", "ok", "healthy"}:
            return f"Conta com token invalido ({token_status})."

        permission_status = str(context.get("permission_status") or "unknown").lower()
        if ProductionConfig.REQUIRE_MULTIUSER_VALID_PERMISSIONS and permission_status not in {"valid", "ok", "healthy"}:
            return f"Permissoes de API invalidas ({permission_status})."

        reconciliation_status = str(context.get("reconciliation_status") or "unknown").lower()
        if ProductionConfig.REQUIRE_MULTIUSER_RECONCILIATION_OK and reconciliation_status not in {"ok", "healthy", "valid"}:
            return f"Reconciliação quebrada ({reconciliation_status})."

        governance_mode = str(context.get("governance_mode") or "").lower()
        governance_blocked = bool(context.get("governance_blocked")) or governance_mode == "blocked"
        if governance_blocked:
            return context.get("governance_block_reason") or "Conta bloqueada por governance/risk."

        allowed_symbols = {str(item).upper() for item in (context.get("allowed_symbols") or [])}
        if symbol and allowed_symbols and str(symbol).upper() not in allowed_symbols:
            return f"Simbolo {symbol} nao permitido para esta conta."

        allowed_timeframes = {str(item).lower() for item in (context.get("allowed_timeframes") or [])}
        if timeframe and allowed_timeframes and str(timeframe).lower() not in allowed_timeframes:
            return f"Timeframe {timeframe} nao permitido para esta conta."

        return None
