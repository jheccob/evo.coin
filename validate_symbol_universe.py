from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from typing import Dict, List

import config
from backtest import run_backtest


DEFAULT_HORIZONS = {
    30: 2880,
    90: 8640,
    180: 17280,
    365: 35040,
}


def _round(value: float) -> float:
    return round(float(value or 0.0), 4)


def _build_horizon_record(summary: Dict[str, object]) -> Dict[str, object]:
    account = summary.get("account_risk_model", {}) if isinstance(summary, dict) else {}
    return {
        "trades": int(summary.get("trades", 0) or 0),
        "wins": int(summary.get("wins", 0) or 0),
        "losses": int(summary.get("losses", 0) or 0),
        "win_rate_pct": _round(summary.get("win_rate_pct", 0.0)),
        "profit_factor": _round(summary.get("profit_factor", 0.0)),
        "max_drawdown_pct": _round(summary.get("max_drawdown", 0.0)),
        "net_pct": _round(summary.get("net_pct", 0.0)),
        "avg_trade_pct": _round(summary.get("avg_trade_pct", 0.0)),
        "account_return_pct": _round(account.get("return_pct", 0.0)),
    }


def _classify_symbol(records: Dict[str, Dict[str, object]]) -> Dict[str, object]:
    annual = records.get("365", {})
    medium = records.get("180", {})
    oos = records.get("90", {})

    annual_trades = int(annual.get("trades", 0) or 0)
    annual_pf = float(annual.get("profit_factor", 0.0) or 0.0)
    annual_net = float(annual.get("net_pct", 0.0) or 0.0)
    annual_dd = float(annual.get("max_drawdown_pct", 0.0) or 0.0)

    oos_trades = int(oos.get("trades", 0) or 0)
    oos_pf = float(oos.get("profit_factor", 0.0) or 0.0)
    oos_net = float(oos.get("net_pct", 0.0) or 0.0)

    medium_pf = float(medium.get("profit_factor", 0.0) or 0.0)
    medium_net = float(medium.get("net_pct", 0.0) or 0.0)

    if (
        annual_trades >= int(config.ProductionConfig.MIN_PROMOTION_SETUP_TRADES)
        and annual_pf >= float(config.ProductionConfig.MIN_PROMOTION_PROFIT_FACTOR)
        and annual_net > 0.0
        and annual_dd <= float(config.ProductionConfig.MAX_PROMOTION_DRAWDOWN)
        and oos_trades >= int(config.ProductionConfig.MIN_PROMOTION_OOS_TRADES)
        and oos_pf >= float(config.ProductionConfig.MIN_PROMOTION_OOS_PROFIT_FACTOR)
        and oos_net > 0.0
        and medium_pf >= 1.0
        and medium_net > 0.0
    ):
        return {
            "status": "approved",
            "approval_label": "approved",
            "reason": "Passou no anual e no out-of-sample com PF e DD dentro da governanca.",
        }

    if annual_net > 0.0 and annual_pf >= 1.0 and annual_dd <= float(config.ProductionConfig.MAX_PROMOTION_DRAWDOWN):
        return {
            "status": "watchlist",
            "approval_label": "watchlist",
            "reason": "Tem edge parcial, mas ainda falhou nos criterios completos de promocao.",
        }

    return {
        "status": "rejected",
        "approval_label": "rejected",
        "reason": "Nao sustentou edge suficiente nas janelas de validacao.",
    }


def validate_symbol(symbol: str, timeframe: str, use_local_csv: bool, initial_balance: float, risk_pct: float) -> Dict[str, object]:
    records: Dict[str, Dict[str, object]] = {}
    for days, candle_count in DEFAULT_HORIZONS.items():
        _, summary = run_backtest(
            symbol=symbol,
            timeframe=timeframe,
            candles=candle_count,
            fee_pct=config.FEE_PCT,
            verbose=False,
            save_report=False,
            use_local_csv=use_local_csv,
            initial_balance=initial_balance,
            risk_per_trade_pct=risk_pct,
        )
        records[str(days)] = _build_horizon_record(summary)

    verdict = _classify_symbol(records)
    return {
        "symbol": config.normalize_symbol(symbol),
        **verdict,
        "horizons": records,
        "validated_at_utc": datetime.now(UTC).isoformat(),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeframe", default=config.TIMEFRAME)
    parser.add_argument("--use-local-csv", action="store_true", default=False)
    parser.add_argument("--initial-balance", type=float, default=config.ProductionConfig.PAPER_ACCOUNT_BALANCE)
    parser.add_argument("--risk-per-trade-pct", type=float, default=config.RISK_PER_TRADE_PCT)
    parser.add_argument("--symbols", nargs="*", default=config.AppConfig.get_global_validation_symbols())
    parser.add_argument("--output", default=config.SYMBOL_APPROVALS_PATH)
    args = parser.parse_args()

    symbols: List[str] = [config.normalize_symbol(item) for item in (args.symbols or []) if str(item).strip()]
    results: Dict[str, object] = {}
    for symbol in symbols:
        print(f"Validando {symbol} em {args.timeframe}...")
        try:
            result = validate_symbol(
                symbol=symbol,
                timeframe=args.timeframe,
                use_local_csv=bool(args.use_local_csv),
                initial_balance=float(args.initial_balance),
                risk_pct=float(args.risk_per_trade_pct),
            )
        except Exception as exc:
            result = {
                "symbol": symbol,
                "status": "error",
                "approval_label": "error",
                "reason": str(exc),
                "horizons": {},
                "validated_at_utc": datetime.now(UTC).isoformat(),
            }
        results[symbol] = result
        print(f"  -> {result['status']}: {result['reason']}")

    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "timeframe": args.timeframe,
        "use_local_csv": bool(args.use_local_csv),
        "account_model": {
            "initial_balance_usdt": _round(args.initial_balance),
            "risk_per_trade_pct": _round(args.risk_per_trade_pct),
        },
        "symbols": results,
    }

    output_path = str(args.output).strip()
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    print(f"Relatorio salvo em {output_path}")


if __name__ == "__main__":
    main()
