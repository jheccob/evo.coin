import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest
import config


def main() -> None:
    parser = argparse.ArgumentParser(description="Roda backtest e imprime apenas o resumo.")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--candles", type=int, required=True)
    parser.add_argument("--fee-pct", type=float, default=config.FEE_PCT)
    parser.add_argument("--risk-per-trade-pct", type=float, default=config.RISK_PER_TRADE_PCT)
    parser.add_argument("--initial-balance", type=float, default=None)
    parser.add_argument("--execution-profile", default=config.EXECUTION_PROFILE)
    parser.add_argument("--no-save-report", action="store_true")
    args = parser.parse_args()

    _, summary = backtest.run_backtest(
        symbol=args.symbol,
        timeframe=args.timeframe,
        candles=args.candles,
        fee_pct=args.fee_pct,
        testnet=False,
        use_local_csv=True,
        slippage_pct=config.SLIPPAGE_PCT,
        execution_profile=args.execution_profile,
        verbose=False,
        save_report=not args.no_save_report,
        initial_balance=args.initial_balance,
        risk_per_trade_pct=args.risk_per_trade_pct,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
