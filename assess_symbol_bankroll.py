from __future__ import annotations

import argparse
import json
from typing import Dict, List

import config
from config import ExchangeConfig
from services.live_execution_service import LiveExecutionService
from services.risk_management_service import RiskManagementService


def _build_context() -> Dict[str, object]:
    return {
        "user_id": 0,
        "account_id": "env-primary",
        "account_alias": "Primary Env Account",
        "exchange_name": "binanceusdm",
        "exchange": "binanceusdm",
        "use_env_credentials": True,
        "credential_source": "env",
    }


def assess_symbol(symbol: str, bankroll: float, entry_price: float, stop_loss_pct: float, risk_pct: float) -> Dict[str, object]:
    execution_service = LiveExecutionService()
    risk_service = RiskManagementService()
    public_exchange = ExchangeConfig.get_exchange_instance(exchange_name="binanceusdm", testnet=bool(config.TESTNET))
    markets = public_exchange.load_markets() or {}
    resolved_symbol = execution_service._resolve_exchange_symbol(public_exchange, symbol)
    trading_rules = execution_service._extract_market_rules(markets.get(resolved_symbol) or {})
    trading_rules.update(
        {
            "symbol": symbol,
            "exchange_symbol": resolved_symbol,
            "exchange_id": str(getattr(public_exchange, "id", "unknown") or "unknown"),
        }
    )
    resolved_entry_price = float(entry_price)
    if resolved_entry_price <= 0:
        ticker = public_exchange.fetch_ticker(resolved_symbol) or {}
        resolved_entry_price = float(ticker.get("last") or ticker.get("close") or 0.0)
    leverage = float(getattr(config, "LEVERAGE", 1) or 1)
    sizing_mode = str(
        getattr(config.ProductionConfig, "RUNTIME_POSITION_SIZING_MODE", getattr(config, "RUNTIME_POSITION_SIZING_MODE", "fixed_allocation"))
        or "fixed_allocation"
    ).strip().lower()
    margin_allocation_pct = float(
        getattr(
            config.ProductionConfig,
            "RUNTIME_POSITION_MARGIN_ALLOCATION_PCT",
            getattr(config, "RUNTIME_POSITION_MARGIN_ALLOCATION_PCT", 100.0),
        )
        or 0.0
    )
    sizing = risk_service.calculate_position_size(
        account_balance=bankroll,
        entry_price=resolved_entry_price,
        stop_loss_pct=stop_loss_pct,
        risk_pct=risk_pct,
        leverage=leverage,
        sizing_mode=sizing_mode,
        margin_allocation_pct=margin_allocation_pct,
    )
    operability = risk_service.evaluate_symbol_operability(
        entry_price=resolved_entry_price,
        stop_loss_pct=stop_loss_pct,
        risk_pct=risk_pct,
        quantity=float(sizing.get("quantity", 0.0) or 0.0),
        position_notional=float(sizing.get("position_notional", 0.0) or 0.0),
        trading_rules=trading_rules,
        leverage=leverage,
        sizing_mode=sizing_mode,
        margin_allocation_pct=margin_allocation_pct,
        account_balance=bankroll,
        available_balance=bankroll,
    )
    return {
        "symbol": symbol,
        "bankroll": round(float(bankroll), 4),
        "entry_price": round(float(resolved_entry_price), 6),
        "stop_loss_pct": round(float(stop_loss_pct), 4),
        "risk_per_trade_pct": round(float(risk_pct), 4),
        "leverage": round(leverage, 4),
        "position_sizing_mode": sizing_mode,
        "position_margin_allocation_pct": round(margin_allocation_pct, 4),
        "sizing": sizing,
        "trading_rules": trading_rules,
        "operable": bool(operability.get("allowed", False)),
        "operability": operability,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=config.AppConfig.get_supported_pairs())
    parser.add_argument("--bankroll", type=float, default=10.0)
    parser.add_argument("--entry-price", type=float, default=0.0)
    parser.add_argument("--stop-loss-pct", type=float, default=config.LONG_STOP_LOSS_PCT)
    parser.add_argument("--risk-per-trade-pct", type=float, default=config.RISK_PER_TRADE_PCT)
    args = parser.parse_args()

    results: List[Dict[str, object]] = []
    for symbol in args.symbols:
        try:
            result = assess_symbol(
                symbol=symbol,
                bankroll=float(args.bankroll),
                entry_price=float(args.entry_price),
                stop_loss_pct=float(args.stop_loss_pct),
                risk_pct=float(args.risk_per_trade_pct),
            )
        except Exception as exc:
            result = {
                "symbol": symbol,
                "error": str(exc),
            }
        results.append(result)

    print(json.dumps(results, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
