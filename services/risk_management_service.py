import logging
from typing import Dict, Optional

from config import ProductionConfig
from database.database import db as runtime_db

logger = logging.getLogger(__name__)


class RiskManagementService:
    def __init__(self, database=None):
        self.database = database or runtime_db

    def calculate_position_size(
        self,
        account_balance: float,
        entry_price: float,
        stop_loss_pct: float,
        risk_pct: float,
    ) -> Dict[str, float]:
        normalized_stop_loss_pct = self._normalize_pct(stop_loss_pct)
        resolved_balance = float(account_balance or 0.0)
        resolved_entry = float(entry_price or 0.0)
        resolved_risk_pct = max(float(risk_pct or 0.0), 0.0)

        if resolved_balance <= 0 or resolved_entry <= 0 or normalized_stop_loss_pct <= 0 or resolved_risk_pct <= 0:
            return {
                "risk_amount": 0.0,
                "position_notional": 0.0,
                "quantity": 0.0,
                "stop_loss_pct": round(normalized_stop_loss_pct * 100, 4),
                "stop_loss_price": 0.0,
            }

        risk_amount = resolved_balance * (resolved_risk_pct / 100.0)
        position_notional = risk_amount / normalized_stop_loss_pct
        quantity = position_notional / resolved_entry if resolved_entry > 0 else 0.0
        stop_loss_price = resolved_entry * (1 - normalized_stop_loss_pct)

        return {
            "risk_amount": round(risk_amount, 2),
            "position_notional": round(position_notional, 2),
            "quantity": round(quantity, 6),
            "stop_loss_pct": round(normalized_stop_loss_pct * 100, 4),
            "stop_loss_price": round(stop_loss_price, 6),
        }

    def evaluate_risk_engine(
        self,
        entry_price: float,
        stop_loss_pct: float,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        account_balance: Optional[float] = None,
        risk_per_trade_pct: Optional[float] = None,
        max_open_trades: Optional[int] = None,
        max_open_trades_per_symbol: Optional[int] = None,
        max_portfolio_open_risk_pct: Optional[float] = None,
        runtime_allowed: bool = True,
        runtime_block_reason: Optional[str] = None,
        regime_allowed: bool = True,
        regime_reason: Optional[str] = None,
        system_health_ok: bool = True,
        system_health_reason: Optional[str] = None,
        portfolio_summary: Optional[Dict] = None,
        symbol_portfolio_summary: Optional[Dict] = None,
        circuit_breaker: Optional[Dict] = None,
        drawdown_summary: Optional[Dict] = None,
    ) -> Dict:
        resolved_account_balance = float(account_balance or ProductionConfig.PAPER_ACCOUNT_BALANCE)
        base_risk_per_trade_pct = float(risk_per_trade_pct or ProductionConfig.RISK_PER_TRADE_PCT)
        resolved_max_open_trades = int(max_open_trades or ProductionConfig.MAX_OPEN_PAPER_TRADES)
        resolved_max_open_trades_per_symbol = int(
            max_open_trades_per_symbol or ProductionConfig.MAX_OPEN_PAPER_TRADES_PER_SYMBOL
        )
        resolved_max_portfolio_open_risk_pct = float(
            max_portfolio_open_risk_pct or ProductionConfig.MAX_PORTFOLIO_OPEN_RISK_PCT
        )
        reduced_multiplier = min(max(float(ProductionConfig.RISK_REDUCED_MODE_MULTIPLIER or 0.5), 0.0), 1.0)

        normalized_stop_loss_pct = self._normalize_pct(stop_loss_pct)
        portfolio_summary = portfolio_summary or self.database.get_open_portfolio_risk_summary()
        symbol_portfolio_summary = symbol_portfolio_summary or self.database.get_open_portfolio_risk_summary(symbol=symbol)
        circuit_breaker = circuit_breaker or self.evaluate_circuit_breaker(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        drawdown_summary = drawdown_summary or self.database.get_paper_drawdown_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )

        open_trades = int(portfolio_summary.get("open_trades", 0) or 0)
        symbol_open_trades = int(symbol_portfolio_summary.get("open_trades", 0) or 0)
        total_open_risk_pct = float(portfolio_summary.get("total_open_risk_pct", 0.0) or 0.0)
        daily_realized_pnl_pct = float(circuit_breaker.get("daily_realized_pnl_pct", 0.0) or 0.0)
        consecutive_losses = int(circuit_breaker.get("consecutive_losses", 0) or 0)
        current_drawdown_pct = float(drawdown_summary.get("current_drawdown_pct", 0.0) or 0.0)
        max_drawdown_pct = float(drawdown_summary.get("max_drawdown_pct", 0.0) or 0.0)

        notes = []
        risk_mode = "normal"
        risk_status = "approved"
        risk_reason = ""

        daily_loss_guard = {
            "status": "ok",
            "triggered": False,
            "current_pct": round(daily_realized_pnl_pct, 4),
            "limit_pct": float(ProductionConfig.MAX_DAILY_PAPER_LOSS_PCT),
        }
        drawdown_guard = {
            "status": "ok",
            "triggered": False,
            "current_pct": round(current_drawdown_pct, 4),
            "warning_pct": float(ProductionConfig.RISK_DRAWDOWN_WARNING_PCT),
            "block_pct": float(ProductionConfig.RISK_DRAWDOWN_BLOCK_PCT),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
        }
        streak_guard = {
            "status": "ok",
            "triggered": False,
            "consecutive_losses": consecutive_losses,
            "warning_threshold": int(ProductionConfig.RISK_STREAK_REDUCTION_THRESHOLD),
            "block_threshold": int(ProductionConfig.MAX_CONSECUTIVE_PAPER_LOSSES),
        }
        exposure_guard = {
            "status": "ok",
            "triggered": False,
            "open_trades": open_trades,
            "symbol_open_trades": symbol_open_trades,
            "total_open_risk_pct": round(total_open_risk_pct, 4),
            "max_open_trades": resolved_max_open_trades,
            "max_open_trades_per_symbol": resolved_max_open_trades_per_symbol,
            "max_portfolio_open_risk_pct": round(resolved_max_portfolio_open_risk_pct, 4),
        }
        system_health_guard = {
            "status": "ok",
            "triggered": False,
            "runtime_allowed": bool(runtime_allowed),
            "regime_allowed": bool(regime_allowed),
            "system_health_ok": bool(system_health_ok),
        }

        if entry_price <= 0:
            return self._blocked_plan(
                "Preco de entrada invalido para calcular o plano de risco.",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
            )

        if normalized_stop_loss_pct <= 0:
            return self._blocked_plan(
                "Setup sem stop loss valido. Operacao bloqueada por risco.",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
            )

        if not runtime_allowed:
            system_health_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                runtime_block_reason or "Runtime bloqueado pela governanca.",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                system_health_guard=system_health_guard,
            )

        if not system_health_ok:
            system_health_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                system_health_reason or "Saude do sistema bloqueou novas entradas.",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                system_health_guard=system_health_guard,
            )

        if not regime_allowed:
            system_health_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                regime_reason or "Regime de mercado incompatível com a operacao.",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                system_health_guard=system_health_guard,
            )

        if not circuit_breaker.get("allowed", True):
            if str(circuit_breaker.get("status")) == "daily_loss_limit":
                daily_loss_guard.update({"status": "blocked", "triggered": True})
            else:
                streak_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                circuit_breaker.get("reason", "Circuit breaker de risco ativo."),
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                daily_loss_guard=daily_loss_guard,
                streak_guard=streak_guard,
                system_health_guard=system_health_guard,
            )

        if open_trades >= resolved_max_open_trades:
            exposure_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                f"Limite de trades abertos atingido ({open_trades}/{resolved_max_open_trades}).",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                exposure_guard=exposure_guard,
                system_health_guard=system_health_guard,
            )

        if symbol_open_trades >= resolved_max_open_trades_per_symbol:
            exposure_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                f"Limite de exposicao por ativo atingido ({symbol_open_trades}/{resolved_max_open_trades_per_symbol}).",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                exposure_guard=exposure_guard,
                system_health_guard=system_health_guard,
            )

        if current_drawdown_pct >= float(ProductionConfig.RISK_DRAWDOWN_BLOCK_PCT):
            drawdown_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                (
                    f"Drawdown corrente de {current_drawdown_pct:.2f}% acima do limite "
                    f"de {ProductionConfig.RISK_DRAWDOWN_BLOCK_PCT:.2f}%."
                ),
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                drawdown_guard=drawdown_guard,
                system_health_guard=system_health_guard,
            )

        if current_drawdown_pct >= float(ProductionConfig.RISK_DRAWDOWN_WARNING_PCT):
            drawdown_guard.update({"status": "warning", "triggered": True})
            risk_mode = "reduced"
            risk_status = "reduced"
            notes.append(
                f"Drawdown em alerta ({current_drawdown_pct:.2f}%). Risco por trade reduzido."
            )

        if consecutive_losses >= int(ProductionConfig.RISK_STREAK_REDUCTION_THRESHOLD):
            streak_guard.update({"status": "warning", "triggered": True})
            risk_mode = "reduced"
            risk_status = "reduced"
            notes.append(
                f"Losing streak de {consecutive_losses} trades. Modo de risco reduzido ativado."
            )

        effective_risk_per_trade_pct = base_risk_per_trade_pct
        if risk_mode == "reduced":
            effective_risk_per_trade_pct *= reduced_multiplier

        remaining_portfolio_risk_pct = resolved_max_portfolio_open_risk_pct - total_open_risk_pct
        if remaining_portfolio_risk_pct <= 0:
            exposure_guard.update({"status": "blocked", "triggered": True})
            return self._blocked_plan(
                "Risco aberto do portfolio acima do limite permitido.",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                exposure_guard=exposure_guard,
                system_health_guard=system_health_guard,
            )

        size_reduced = False
        if effective_risk_per_trade_pct > remaining_portfolio_risk_pct:
            effective_risk_per_trade_pct = remaining_portfolio_risk_pct
            size_reduced = True
            risk_mode = "reduced"
            risk_status = "reduced"
            exposure_guard.update({"status": "warning", "triggered": True})
            notes.append("Size reduzido para respeitar o limite de risco aberto do portfolio.")

        if risk_mode == "reduced" and effective_risk_per_trade_pct < base_risk_per_trade_pct:
            size_reduced = True

        sizing = self.calculate_position_size(
            account_balance=resolved_account_balance,
            entry_price=entry_price,
            stop_loss_pct=normalized_stop_loss_pct,
            risk_pct=effective_risk_per_trade_pct,
        )

        if sizing["quantity"] <= 0 or sizing["position_notional"] <= 0:
            return self._blocked_plan(
                "Nao foi possivel calcular um tamanho de posicao valido.",
                portfolio_summary=portfolio_summary,
                circuit_breaker=circuit_breaker,
                drawdown_summary=drawdown_summary,
                symbol_open_trades=symbol_open_trades,
                risk_mode="blocked",
                risk_status="blocked",
                system_health_guard=system_health_guard,
            )

        risk_reason = notes[0] if notes else ""
        return {
            "risk_permission": True,
            "risk_status": risk_status,
            "risk_reason": risk_reason,
            "allowed": True,
            "reason": risk_reason,
            "risk_mode": risk_mode,
            "size_reduced": bool(size_reduced),
            "allowed_position_size": sizing["quantity"],
            "position_size": sizing["quantity"],
            "account_reference_balance": round(resolved_account_balance, 2),
            "base_risk_per_trade_pct": round(base_risk_per_trade_pct, 4),
            "risk_per_trade_pct": round(effective_risk_per_trade_pct, 4),
            "max_risk_per_trade": round(base_risk_per_trade_pct, 4),
            "risk_amount": sizing["risk_amount"],
            "stop_loss_pct": sizing["stop_loss_pct"],
            "stop_loss_price": sizing["stop_loss_price"],
            "position_notional": sizing["position_notional"],
            "quantity": sizing["quantity"],
            "portfolio_open_trades": open_trades,
            "symbol_open_trades": symbol_open_trades,
            "portfolio_open_risk_pct": round(total_open_risk_pct, 4),
            "max_open_trades": resolved_max_open_trades,
            "max_open_trades_per_symbol": resolved_max_open_trades_per_symbol,
            "max_portfolio_open_risk_pct": round(resolved_max_portfolio_open_risk_pct, 4),
            "circuit_breaker_allowed": bool(circuit_breaker.get("allowed", True)),
            "daily_closed_trades": int(circuit_breaker.get("daily_closed_trades", 0) or 0),
            "daily_realized_pnl_pct": round(daily_realized_pnl_pct, 4),
            "consecutive_losses": consecutive_losses,
            "current_drawdown_pct": round(current_drawdown_pct, 4),
            "max_drawdown_pct": round(max_drawdown_pct, 4),
            "daily_loss_guard": daily_loss_guard,
            "drawdown_guard": drawdown_guard,
            "streak_guard": streak_guard,
            "exposure_guard": exposure_guard,
            "system_health_guard": system_health_guard,
            "notes": notes,
        }

    def build_trade_plan(
        self,
        entry_price: float,
        stop_loss_pct: float,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
        account_balance: Optional[float] = None,
        risk_per_trade_pct: Optional[float] = None,
        max_open_trades: Optional[int] = None,
        max_portfolio_open_risk_pct: Optional[float] = None,
        runtime_allowed: bool = True,
        runtime_block_reason: Optional[str] = None,
        regime_allowed: bool = True,
        regime_reason: Optional[str] = None,
        system_health_ok: bool = True,
        system_health_reason: Optional[str] = None,
    ) -> Dict:
        # Compatibility alias kept for older callers and tests.
        return self.evaluate_risk_engine(
            entry_price=entry_price,
            stop_loss_pct=stop_loss_pct,
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
            account_balance=account_balance,
            risk_per_trade_pct=risk_per_trade_pct,
            max_open_trades=max_open_trades,
            max_portfolio_open_risk_pct=max_portfolio_open_risk_pct,
            runtime_allowed=runtime_allowed,
            runtime_block_reason=runtime_block_reason,
            regime_allowed=regime_allowed,
            regime_reason=regime_reason,
            system_health_ok=system_health_ok,
            system_health_reason=system_health_reason,
        )

    def get_portfolio_risk_summary(self) -> Dict:
        summary = self.database.get_open_portfolio_risk_summary()
        circuit_breaker = self.evaluate_circuit_breaker()
        drawdown_summary = self.database.get_paper_drawdown_summary()
        risk_mode = "blocked" if not circuit_breaker.get("allowed", True) else "normal"
        if risk_mode != "blocked":
            if float(drawdown_summary.get("current_drawdown_pct", 0.0) or 0.0) >= float(
                ProductionConfig.RISK_DRAWDOWN_WARNING_PCT
            ) or int(circuit_breaker.get("consecutive_losses", 0) or 0) >= int(
                ProductionConfig.RISK_STREAK_REDUCTION_THRESHOLD
            ):
                risk_mode = "reduced"
        return {
            "open_trades": int(summary.get("open_trades", 0) or 0),
            "total_open_risk_pct": round(float(summary.get("total_open_risk_pct", 0.0) or 0.0), 4),
            "total_open_risk_amount": round(float(summary.get("total_open_risk_amount", 0.0) or 0.0), 2),
            "total_open_position_notional": round(
                float(summary.get("total_open_position_notional", 0.0) or 0.0),
                2,
            ),
            "max_open_trades": ProductionConfig.MAX_OPEN_PAPER_TRADES,
            "max_open_trades_per_symbol": ProductionConfig.MAX_OPEN_PAPER_TRADES_PER_SYMBOL,
            "max_portfolio_open_risk_pct": ProductionConfig.MAX_PORTFOLIO_OPEN_RISK_PCT,
            "risk_mode": risk_mode,
            "circuit_breaker_allowed": bool(circuit_breaker.get("allowed", True)),
            "circuit_breaker_reason": circuit_breaker.get("reason", ""),
            "daily_closed_trades": int(circuit_breaker.get("daily_closed_trades", 0) or 0),
            "daily_realized_pnl": round(float(circuit_breaker.get("daily_realized_pnl", 0.0) or 0.0), 2),
            "daily_realized_pnl_pct": round(float(circuit_breaker.get("daily_realized_pnl_pct", 0.0) or 0.0), 4),
            "consecutive_losses": int(circuit_breaker.get("consecutive_losses", 0) or 0),
            "current_drawdown_pct": round(float(drawdown_summary.get("current_drawdown_pct", 0.0) or 0.0), 4),
            "max_drawdown_pct": round(float(drawdown_summary.get("max_drawdown_pct", 0.0) or 0.0), 4),
            "max_daily_paper_loss_pct": ProductionConfig.MAX_DAILY_PAPER_LOSS_PCT,
            "max_consecutive_paper_losses": ProductionConfig.MAX_CONSECUTIVE_PAPER_LOSSES,
        }

    def evaluate_circuit_breaker(
        self,
        symbol: str = None,
        timeframe: str = None,
        strategy_version: str = None,
    ) -> Dict:
        if not ProductionConfig.ENABLE_RISK_CIRCUIT_BREAKER:
            return {
                "allowed": True,
                "reason": "",
                "status": "disabled",
                "daily_closed_trades": 0,
                "daily_realized_pnl": 0.0,
                "daily_realized_pnl_pct": 0.0,
                "consecutive_losses": 0,
            }

        daily_summary = self.database.get_daily_paper_guardrail_summary(
            symbol=symbol,
            timeframe=timeframe,
            strategy_version=strategy_version,
        )
        daily_realized_pnl_pct = float(daily_summary.get("realized_pnl_pct", 0.0) or 0.0)
        consecutive_losses = int(daily_summary.get("consecutive_losses", 0) or 0)

        if daily_realized_pnl_pct <= -float(ProductionConfig.MAX_DAILY_PAPER_LOSS_PCT):
            return {
                "allowed": False,
                "reason": (
                    f"Circuit breaker ativo: perda diaria de {abs(daily_realized_pnl_pct):.2f}% "
                    f"(limite {ProductionConfig.MAX_DAILY_PAPER_LOSS_PCT:.2f}%)."
                ),
                "status": "daily_loss_limit",
                "daily_closed_trades": int(daily_summary.get("closed_trades", 0) or 0),
                "daily_realized_pnl": float(daily_summary.get("realized_pnl", 0.0) or 0.0),
                "daily_realized_pnl_pct": daily_realized_pnl_pct,
                "consecutive_losses": consecutive_losses,
            }

        if consecutive_losses >= int(ProductionConfig.MAX_CONSECUTIVE_PAPER_LOSSES):
            return {
                "allowed": False,
                "reason": (
                    f"Circuit breaker ativo: {consecutive_losses} losses consecutivos "
                    f"(limite {ProductionConfig.MAX_CONSECUTIVE_PAPER_LOSSES})."
                ),
                "status": "loss_streak_limit",
                "daily_closed_trades": int(daily_summary.get("closed_trades", 0) or 0),
                "daily_realized_pnl": float(daily_summary.get("realized_pnl", 0.0) or 0.0),
                "daily_realized_pnl_pct": daily_realized_pnl_pct,
                "consecutive_losses": consecutive_losses,
            }

        return {
            "allowed": True,
            "reason": "",
            "status": "healthy",
            "daily_closed_trades": int(daily_summary.get("closed_trades", 0) or 0),
            "daily_realized_pnl": float(daily_summary.get("realized_pnl", 0.0) or 0.0),
            "daily_realized_pnl_pct": daily_realized_pnl_pct,
            "consecutive_losses": consecutive_losses,
        }

    def _blocked_plan(
        self,
        reason: str,
        portfolio_summary: Dict,
        circuit_breaker: Dict = None,
        drawdown_summary: Dict = None,
        symbol_open_trades: int = 0,
        risk_mode: str = "blocked",
        risk_status: str = "blocked",
        daily_loss_guard: Optional[Dict] = None,
        drawdown_guard: Optional[Dict] = None,
        streak_guard: Optional[Dict] = None,
        exposure_guard: Optional[Dict] = None,
        system_health_guard: Optional[Dict] = None,
    ) -> Dict:
        circuit_breaker = circuit_breaker or {}
        drawdown_summary = drawdown_summary or {}
        return {
            "risk_permission": False,
            "risk_status": risk_status,
            "risk_reason": reason,
            "allowed": False,
            "reason": reason,
            "risk_mode": risk_mode,
            "size_reduced": False,
            "allowed_position_size": 0.0,
            "position_size": 0.0,
            "risk_per_trade_pct": round(float(ProductionConfig.RISK_PER_TRADE_PCT), 4),
            "max_risk_per_trade": round(float(ProductionConfig.RISK_PER_TRADE_PCT), 4),
            "risk_amount": 0.0,
            "position_notional": 0.0,
            "quantity": 0.0,
            "portfolio_open_trades": int(portfolio_summary.get("open_trades", 0) or 0),
            "symbol_open_trades": int(symbol_open_trades or 0),
            "portfolio_open_risk_pct": round(float(portfolio_summary.get("total_open_risk_pct", 0.0) or 0.0), 4),
            "max_open_trades": ProductionConfig.MAX_OPEN_PAPER_TRADES,
            "max_open_trades_per_symbol": ProductionConfig.MAX_OPEN_PAPER_TRADES_PER_SYMBOL,
            "max_portfolio_open_risk_pct": ProductionConfig.MAX_PORTFOLIO_OPEN_RISK_PCT,
            "circuit_breaker_allowed": bool(circuit_breaker.get("allowed", True)),
            "daily_closed_trades": int(circuit_breaker.get("daily_closed_trades", 0) or 0),
            "daily_realized_pnl_pct": round(float(circuit_breaker.get("daily_realized_pnl_pct", 0.0) or 0.0), 4),
            "consecutive_losses": int(circuit_breaker.get("consecutive_losses", 0) or 0),
            "current_drawdown_pct": round(float(drawdown_summary.get("current_drawdown_pct", 0.0) or 0.0), 4),
            "max_drawdown_pct": round(float(drawdown_summary.get("max_drawdown_pct", 0.0) or 0.0), 4),
            "daily_loss_guard": daily_loss_guard
            or {
                "status": "ok",
                "triggered": False,
                "current_pct": round(float(circuit_breaker.get("daily_realized_pnl_pct", 0.0) or 0.0), 4),
                "limit_pct": float(ProductionConfig.MAX_DAILY_PAPER_LOSS_PCT),
            },
            "drawdown_guard": drawdown_guard
            or {
                "status": "ok",
                "triggered": False,
                "current_pct": round(float(drawdown_summary.get("current_drawdown_pct", 0.0) or 0.0), 4),
                "warning_pct": float(ProductionConfig.RISK_DRAWDOWN_WARNING_PCT),
                "block_pct": float(ProductionConfig.RISK_DRAWDOWN_BLOCK_PCT),
                "max_drawdown_pct": round(float(drawdown_summary.get("max_drawdown_pct", 0.0) or 0.0), 4),
            },
            "streak_guard": streak_guard
            or {
                "status": "ok",
                "triggered": False,
                "consecutive_losses": int(circuit_breaker.get("consecutive_losses", 0) or 0),
                "warning_threshold": int(ProductionConfig.RISK_STREAK_REDUCTION_THRESHOLD),
                "block_threshold": int(ProductionConfig.MAX_CONSECUTIVE_PAPER_LOSSES),
            },
            "exposure_guard": exposure_guard
            or {
                "status": "ok",
                "triggered": False,
                "open_trades": int(portfolio_summary.get("open_trades", 0) or 0),
                "symbol_open_trades": int(symbol_open_trades or 0),
                "total_open_risk_pct": round(float(portfolio_summary.get("total_open_risk_pct", 0.0) or 0.0), 4),
                "max_open_trades": ProductionConfig.MAX_OPEN_PAPER_TRADES,
                "max_open_trades_per_symbol": ProductionConfig.MAX_OPEN_PAPER_TRADES_PER_SYMBOL,
                "max_portfolio_open_risk_pct": float(ProductionConfig.MAX_PORTFOLIO_OPEN_RISK_PCT),
            },
            "system_health_guard": system_health_guard
            or {
                "status": "ok",
                "triggered": False,
                "runtime_allowed": True,
                "regime_allowed": True,
                "system_health_ok": True,
            },
            "notes": [reason],
        }

    def _normalize_pct(self, value: Optional[float]) -> float:
        raw_value = float(value or 0.0)
        return raw_value / 100 if raw_value > 1 else raw_value
