from __future__ import annotations

from dataclasses import dataclass
from typing import Dict

import config


@dataclass
class RiskManagementService:
    risk_per_trade_pct: float = config.RISK_PER_TRADE_PCT
    max_open_trades: int = config.MAX_OPEN_TRADES

    def evaluate_risk_engine(
        self,
        entry_price: float,
        stop_loss_price: float,
        account_balance: float,
        open_trades: int = 0,
    ) -> Dict:
        if account_balance <= 0:
            return {"allowed": False, "reason": "saldo invalido", "quantity": 0.0}
        if entry_price <= 0 or stop_loss_price <= 0:
            return {"allowed": False, "reason": "precos invalidos", "quantity": 0.0}
        if open_trades >= self.max_open_trades:
            return {"allowed": False, "reason": "maximo de operacoes abertas atingido", "quantity": 0.0}

        risk_amount = account_balance * (self.risk_per_trade_pct / 100)
        stop_distance = abs(entry_price - stop_loss_price)
        if stop_distance <= 0:
            return {"allowed": False, "reason": "distancia de stop invalida", "quantity": 0.0}

        quantity = risk_amount / stop_distance
        return {
            "allowed": True,
            "reason": "ok",
            "risk_amount": round(risk_amount, 8),
            "stop_distance": round(stop_distance, 8),
            "quantity": round(quantity, 6),
        }
