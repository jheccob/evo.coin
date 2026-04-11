from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict

import config
from market_data import fetch_candles
from position_manager import create_position
from risk_management_service import RiskManagementService
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal


@dataclass
class FuturesTradingRefactored:
    symbol: str = config.SYMBOL
    timeframe: str = config.TIMEFRAME
    leverage: int = config.LEVERAGE
    testnet: bool = config.TESTNET
    params: StrategyParams = field(default_factory=StrategyParams)
    risk_service: RiskManagementService = field(default_factory=RiskManagementService)

    def generate_signal(self) -> Dict:
        df = fetch_candles(self.symbol, self.timeframe, limit=max(config.LIMIT, 200), testnet=self.testnet)
        df = calculate_indicators(df, self.params)
        return generate_entry_signal(df, self.params)

    def build_trade_plan(self, account_balance: float, open_trades: int = 0) -> Dict:
        signal = self.generate_signal()
        if signal.get("signal") not in {"buy", "sell"}:
            return {"allowed": False, "message": signal.get("reason", "sem sinal"), "signal": signal}

        entry_price = float(signal["entry_price"])
        if signal["signal"] == "buy":
            stop_loss_price = entry_price * (1 - config.LONG_STOP_LOSS_PCT / 100)
        else:
            stop_loss_price = entry_price * (1 + config.SHORT_STOP_LOSS_PCT / 100)

        risk = self.risk_service.evaluate_risk_engine(
            entry_price=entry_price,
            stop_loss_price=stop_loss_price,
            account_balance=account_balance,
            open_trades=open_trades,
        )
        return {
            "allowed": bool(risk.get("allowed", False)),
            "signal": signal,
            "risk": risk,
            "entry_price": entry_price,
            "stop_loss_price": stop_loss_price,
            "leverage": self.leverage,
        }

    def create_runtime_position(self, signal: Dict, timestamp):
        return create_position(
            signal=signal["signal"],
            entry_price=float(signal["entry_price"]),
            timestamp=timestamp,
            atr=float(signal["atr"]),
        )
