import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import backtest
import config


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _print_window(label: str, trades: list[dict], side: str) -> None:
    side_trades = [trade for trade in trades if trade.get("side") == side]
    wins = [trade for trade in side_trades if float(trade.get("net_pct", 0.0)) > 0]
    losses = [trade for trade in side_trades if float(trade.get("net_pct", 0.0)) <= 0]
    print(f"janela: {label}")
    print(f"trades: {len(side_trades)} wins: {len(wins)} losses: {len(losses)}")
    print(f"win_rate: {(len(wins) / len(side_trades) * 100) if side_trades else 0.0:.2f}%")
    print(f"net: {sum(float(trade.get('net_pct', 0.0)) for trade in side_trades):.4f}%")
    print(f"avg_loser_mfe: {_mean([float(trade.get('mfe_pct', 0.0)) for trade in losses]):.4f}%")
    print(f"avg_loser_mae: {_mean([float(trade.get('mae_pct', 0.0)) for trade in losses]):.4f}%")
    print(f"avg_winner_mfe: {_mean([float(trade.get('mfe_pct', 0.0)) for trade in wins]):.4f}%")
    print()

    setups = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    tokens_wins = Counter()
    tokens_losses = Counter()
    for trade in side_trades:
        setup = trade.get("entry_setup") or "unknown"
        net_pct = float(trade.get("net_pct", 0.0))
        setups[setup]["trades"] += 1
        setups[setup]["net"] += net_pct
        if net_pct > 0:
            setups[setup]["wins"] += 1
        else:
            setups[setup]["losses"] += 1
        reason = str(trade.get("entry_signal_reason") or "")
        bits = reason.split("|", 1)
        if len(bits) == 2:
            tokens = [token.strip() for token in bits[1].split(",") if token.strip()]
            if net_pct > 0:
                tokens_wins.update(tokens)
            else:
                tokens_losses.update(tokens)

    print("por setup:")
    for setup, stats in sorted(setups.items(), key=lambda item: item[1]["net"]):
        win_rate = (stats["wins"] / stats["trades"] * 100) if stats["trades"] else 0.0
        print(
            f"  {setup}: trades={stats['trades']} wins={stats['wins']} "
            f"losses={stats['losses']} win_rate={win_rate:.2f}% net={stats['net']:.4f}%"
        )
        setup_trades = [trade for trade in side_trades if (trade.get("entry_setup") or "unknown") == setup]
        setup_wins = [trade for trade in setup_trades if float(trade.get("net_pct", 0.0)) > 0]
        setup_losses = [trade for trade in setup_trades if float(trade.get("net_pct", 0.0)) <= 0]
        for label_name, items in (("wins", setup_wins), ("losses", setup_losses)):
            print(
                f"    {label_name}: rsi={_mean([float(trade.get('signal_rsi', 0.0) or 0.0) for trade in items]):.2f} "
                f"adx={_mean([float(trade.get('signal_adx', 0.0) or 0.0) for trade in items]):.2f} "
                f"atr_pct={_mean([float(trade.get('signal_atr_pct', 0.0) or 0.0) for trade in items]):.2f} "
                f"context_gap={_mean([float(trade.get('signal_context_gap_pct', 0.0) or 0.0) for trade in items]):.3f} "
                f"trend_strength={_mean([float(trade.get('signal_trend_strength_pct', 0.0) or 0.0) for trade in items]):.3f}"
            )
    print()

    print("tokens que pesam nos losses:")
    top_tokens = sorted(set(tokens_wins) | set(tokens_losses), key=lambda token: tokens_losses[token], reverse=True)[:10]
    for token in top_tokens:
        print(
            f"  {token}: wins={tokens_wins[token]} losses={tokens_losses[token]}"
        )
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Roda backtest e faz diagnostico por lado.")
    parser.add_argument("--symbol", default="BTC/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--candles", type=int, required=True)
    parser.add_argument("--fee-pct", type=float, default=config.FEE_PCT)
    parser.add_argument("--risk-per-trade-pct", type=float, default=config.RISK_PER_TRADE_PCT)
    parser.add_argument("--side", choices=("long", "short"), required=True)
    parser.add_argument("--split-date", default=None)
    args = parser.parse_args()

    trades, summary = backtest.run_backtest(
        symbol=args.symbol,
        timeframe=args.timeframe,
        candles=args.candles,
        fee_pct=args.fee_pct,
        testnet=False,
        use_local_csv=True,
        slippage_pct=config.SLIPPAGE_PCT,
        execution_profile=config.EXECUTION_PROFILE,
        verbose=False,
        save_report=False,
        risk_per_trade_pct=args.risk_per_trade_pct,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print()
    _print_window("completo", trades, args.side)
    if args.split_date:
        older = [trade for trade in trades if str(trade.get("entry_timestamp") or "") < args.split_date]
        newer = [trade for trade in trades if str(trade.get("entry_timestamp") or "") >= args.split_date]
        _print_window(f"antes de {args.split_date}", older, args.side)
        _print_window(f"a partir de {args.split_date}", newer, args.side)


if __name__ == "__main__":
    main()
