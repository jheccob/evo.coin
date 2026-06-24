import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def _pct(value: float) -> str:
    return f"{value:.2f}%"


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _print_analysis(report_path: Path, side: str, trades: list[dict], label: str | None = None) -> None:
    if not trades:
        print(f"nenhum trade encontrado para side={side}")
        return

    wins = [trade for trade in trades if float(trade.get("net_pct", 0.0)) > 0]
    losses = [trade for trade in trades if float(trade.get("net_pct", 0.0)) <= 0]

    print(f"arquivo: {report_path}")
    if label:
        print(f"janela: {label}")
    print(f"lado: {side}")
    print(f"trades: {len(trades)}")
    print(f"wins: {len(wins)}")
    print(f"losses: {len(losses)}")
    print(f"win_rate: {_pct((len(wins) / len(trades)) * 100 if trades else 0.0)}")
    print(f"net: {_pct(sum(float(trade.get('net_pct', 0.0)) for trade in trades))}")
    print(f"avg_mfe: {_pct(_mean([float(trade.get('mfe_pct', 0.0)) for trade in trades]))}")
    print(f"avg_mae: {_pct(_mean([float(trade.get('mae_pct', 0.0)) for trade in trades]))}")
    print()

    setups = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    hours = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "net": 0.0})
    exit_reasons = Counter()
    tokens = Counter()

    for trade in trades:
        net_pct = float(trade.get("net_pct", 0.0))
        setup = trade.get("entry_setup") or "unknown"
        hour = int(trade.get("signal_hour_utc", -1))
        exit_reason = str(trade.get("reason") or "unknown")
        entry_reason = str(trade.get("entry_signal_reason") or "")

        setups[setup]["trades"] += 1
        setups[setup]["net"] += net_pct
        hours[hour]["trades"] += 1
        hours[hour]["net"] += net_pct
        exit_reasons[exit_reason] += 1

        if net_pct > 0:
            setups[setup]["wins"] += 1
            hours[hour]["wins"] += 1
        else:
            setups[setup]["losses"] += 1
            hours[hour]["losses"] += 1

        reason_bits = entry_reason.split("|", 1)
        if len(reason_bits) == 2:
            for token in reason_bits[1].split(","):
                clean = token.strip()
                if clean:
                    tokens[clean] += 1

    print("por setup:")
    for setup, stats in sorted(setups.items(), key=lambda item: item[1]["net"]):
        win_rate = (stats["wins"] / stats["trades"]) * 100 if stats["trades"] else 0.0
        print(
            f"  {setup}: trades={stats['trades']} wins={stats['wins']} "
            f"losses={stats['losses']} win_rate={win_rate:.2f}% net={stats['net']:.4f}%"
        )
    print()

    print("piores horas:")
    for hour, stats in sorted(hours.items(), key=lambda item: item[1]["net"])[:8]:
        win_rate = (stats["wins"] / stats["trades"]) * 100 if stats["trades"] else 0.0
        print(
            f"  {hour:02d} UTC: trades={stats['trades']} wins={stats['wins']} "
            f"losses={stats['losses']} win_rate={win_rate:.2f}% net={stats['net']:.4f}%"
        )
    print()

    print("exit reasons:")
    for reason, count in exit_reasons.most_common():
        print(f"  {reason}: {count}")
    print()

    print("tokens mais comuns:")
    for token, count in tokens.most_common(12):
        print(f"  {token}: {count}")
    print()

    loser_mfe = [float(trade.get("mfe_pct", 0.0)) for trade in losses]
    loser_mae = [float(trade.get("mae_pct", 0.0)) for trade in losses]
    winners_mfe = [float(trade.get("mfe_pct", 0.0)) for trade in wins]
    winners_mae = [float(trade.get("mae_pct", 0.0)) for trade in wins]

    print("qualidade das perdas:")
    print(f"  avg_loser_mfe: {_pct(_mean(loser_mfe))}")
    print(f"  avg_loser_mae: {_pct(_mean(loser_mae))}")
    print(f"  avg_winner_mfe: {_pct(_mean(winners_mfe))}")
    print(f"  avg_winner_mae: {_pct(_mean(winners_mae))}")
    print(f"  losers_mfe_le_0.25: {sum(1 for value in loser_mfe if value <= 0.25)}")
    print(f"  losers_mfe_ge_1.00: {sum(1 for value in loser_mfe if value >= 1.0)}")
    print()

    print("metricas por setup:")
    for setup in sorted(setups):
        setup_trades = [trade for trade in trades if (trade.get("entry_setup") or "unknown") == setup]
        setup_wins = [trade for trade in setup_trades if float(trade.get("net_pct", 0.0)) > 0]
        setup_losses = [trade for trade in setup_trades if float(trade.get("net_pct", 0.0)) <= 0]

        def metric(items: list[dict], key: str) -> float:
            values = [float(item.get(key, 0.0) or 0.0) for item in items if item.get(key) is not None]
            return _mean(values)

        print(f"  {setup}:")
        print(
            f"    wins: count={len(setup_wins)} rsi={metric(setup_wins, 'signal_rsi'):.2f} "
            f"adx={metric(setup_wins, 'signal_adx'):.2f} atr_pct={metric(setup_wins, 'signal_atr_pct'):.2f} "
            f"trend_strength={metric(setup_wins, 'signal_trend_strength_pct'):.3f} "
            f"context_gap={metric(setup_wins, 'signal_context_gap_pct'):.3f}"
        )
        print(
            f"    losses: count={len(setup_losses)} rsi={metric(setup_losses, 'signal_rsi'):.2f} "
            f"adx={metric(setup_losses, 'signal_adx'):.2f} atr_pct={metric(setup_losses, 'signal_atr_pct'):.2f} "
            f"trend_strength={metric(setup_losses, 'signal_trend_strength_pct'):.3f} "
            f"context_gap={metric(setup_losses, 'signal_context_gap_pct'):.3f}"
        )

        token_counter_wins = Counter()
        token_counter_losses = Counter()
        for item in setup_wins:
            reason_bits = str(item.get("entry_signal_reason") or "").split("|", 1)
            if len(reason_bits) == 2:
                for token in reason_bits[1].split(","):
                    clean = token.strip()
                    if clean:
                        token_counter_wins[clean] += 1
        for item in setup_losses:
            reason_bits = str(item.get("entry_signal_reason") or "").split("|", 1)
            if len(reason_bits) == 2:
                for token in reason_bits[1].split(","):
                    clean = token.strip()
                    if clean:
                        token_counter_losses[clean] += 1

        top_tokens = sorted(
            set(token_counter_wins.keys()) | set(token_counter_losses.keys()),
            key=lambda token: max(token_counter_wins[token], token_counter_losses[token]),
            reverse=True,
        )[:8]
        for token in top_tokens:
            win_rate = (token_counter_wins[token] / len(setup_wins) * 100) if setup_wins else 0.0
            loss_rate = (token_counter_losses[token] / len(setup_losses) * 100) if setup_losses else 0.0
            print(
                f"    token {token}: wins={token_counter_wins[token]} ({win_rate:.1f}%) "
                f"losses={token_counter_losses[token]} ({loss_rate:.1f}%)"
            )


def analyze_report(report_path: Path, side: str, split_date: str | None = None) -> None:
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    trades = [trade for trade in payload.get("trades", []) if trade.get("side") == side]
    _print_analysis(report_path, side, trades)
    if split_date:
        older = [trade for trade in trades if str(trade.get("entry_timestamp") or "") < split_date]
        newer = [trade for trade in trades if str(trade.get("entry_timestamp") or "") >= split_date]
        print()
        _print_analysis(report_path, side, older, label=f"antes de {split_date}")
        print()
        _print_analysis(report_path, side, newer, label=f"a partir de {split_date}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Analisa um relatorio de backtest por lado.")
    parser.add_argument("report", help="caminho do arquivo JSON do backtest")
    parser.add_argument("--side", choices=("long", "short"), required=True)
    parser.add_argument("--split-date", help="separa a analise em duas janelas pelo entry_timestamp ISO")
    args = parser.parse_args()
    analyze_report(Path(args.report), args.side, split_date=args.split_date)


if __name__ == "__main__":
    main()
