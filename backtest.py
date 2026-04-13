from __future__ import annotations

import argparse
from datetime import timezone

import pandas as pd

import config
from market_data import fetch_historical_candles
from position_manager import create_position, evaluate_open_position
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal


def format_timestamp(ts):
    if isinstance(ts, pd.Timestamp):
        return ts.tz_convert(timezone.utc).isoformat() if ts.tzinfo else ts.tz_localize(timezone.utc).isoformat()
    return str(ts)


def summarize_trades(trades):
    wins = sum(1 for t in trades if t["net_pct"] > 0)
    losses = len(trades) - wins
    net_pct = sum(t["net_pct"] for t in trades)
    return {
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round((wins / len(trades) * 100), 2) if trades else 0.0,
        "net_pct": round(net_pct, 4),
        "avg_trade_pct": round(net_pct / len(trades), 4) if trades else 0.0,
    }


def run_backtest(
    symbol: str,
    timeframe: str,
    candles: int,
    fee_pct: float,
    testnet: bool = False,
    use_local_csv: bool = False,
    preloaded_df: pd.DataFrame | None = None,
):
    params = StrategyParams()

    if preloaded_df is not None:
        df = preloaded_df.copy()
    else:
        if use_local_csv:
            print("Aviso: modo atual do backtest ignora CSV local e usa historico direto da exchange.")
        df = fetch_historical_candles(symbol, timeframe, total_limit=candles, testnet=testnet)

    df = calculate_indicators(df, params)

    position = None
    pending_signal = None
    trades = []
    realized_partial_pct = 0.0

    start_index = max(
        params.ema_trend + 5,
        params.rsi_period + 5,
        params.atr_period + 5,
    )

    for i in range(start_index, len(df) - 1):
        row = df.iloc[i]
        next_row = df.iloc[i + 1]

        # gerenciamento da posição aberta
        if position is not None:
            management = evaluate_open_position(
                position,
                current_price=float(row["close"]),
                timestamp=row["timestamp"],
            )

            if management["action"] == "partial":
                partial_price = float(row["close"])
                side = position["side"]
                entry_price = float(position["entry_price"])

                partial_gross_pct = (
                    (partial_price - entry_price) / entry_price * 100
                    if side == "long"
                    else (entry_price - partial_price) / entry_price * 100
                )

                # realiza 50% da posição
                realized_partial_pct += partial_gross_pct * 0.5
                position = management["position"]

            elif management["action"] == "close":
                trade = management["closed_position"]
                trade["entry_timestamp"] = format_timestamp(trade["entry_timestamp"])
                trade["exit_timestamp"] = format_timestamp(trade["exit_timestamp"])

                # se houve parcial, o fechamento final representa só os 50% restantes
                if position.get("partial_taken", False):
                    final_gross_pct = trade["gross_pct"] * 0.5 + realized_partial_pct
                else:
                    final_gross_pct = trade["gross_pct"]

                trade["gross_pct"] = final_gross_pct
                trade["net_pct"] = final_gross_pct - fee_pct

                trades.append(trade)
                position = None
                realized_partial_pct = 0.0

            else:
                position = management["position"]

        # geração do sinal
        signal = generate_entry_signal(df.iloc[: i + 1], params)

        if position is None and signal.get("signal") in {"buy", "sell"}:
            pending_signal = signal
        elif position is not None:
            pending_signal = None

        # abertura da posição
        if position is None and pending_signal is not None:
            entry_price = (
                float(next_row["open"])
                if config.USE_NEXT_CANDLE_OPEN_FOR_BACKTEST
                else float(row["close"])
            )

            position = create_position(
                signal=pending_signal["signal"],
                entry_price=entry_price,
                timestamp=(
                    next_row["timestamp"]
                    if config.USE_NEXT_CANDLE_OPEN_FOR_BACKTEST
                    else row["timestamp"]
                ),
                atr=float(pending_signal["atr"]),
            )

            pending_signal = None

    # encerra posição aberta no último candle
    if position is not None:
        last_row = df.iloc[-1]
        exit_price = float(last_row["close"])

        gross_pct = (
            (exit_price - position["entry_price"]) / position["entry_price"] * 100
            if position["side"] == "long"
            else (position["entry_price"] - exit_price) / position["entry_price"] * 100
        )

        if position.get("partial_taken", False):
            final_gross_pct = gross_pct * 0.5 + realized_partial_pct
        else:
            final_gross_pct = gross_pct

        trades.append(
            {
                "side": position["side"],
                "entry_price": float(position["entry_price"]),
                "exit_price": exit_price,
                "entry_timestamp": format_timestamp(position["entry_timestamp"]),
                "exit_timestamp": format_timestamp(last_row["timestamp"]),
                "best_price": float(position["best_price"]),
                "gross_pct": final_gross_pct,
                "net_pct": final_gross_pct - fee_pct,
                "reason": "encerramento_backtest",
            }
        )

    summary = summarize_trades(trades)
    print("Resumo:", summary)

    if trades:
        print("Primeiro trade:", trades[0])
        print("Ultimo trade:", trades[-1])

    return trades, summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol", default=config.SYMBOL)
    parser.add_argument("--timeframe", default=config.TIMEFRAME)
    parser.add_argument("--candles", type=int, default=3000)
    parser.add_argument("--fee-pct", type=float, default=config.FEE_PCT)
    parser.add_argument("--testnet", action="store_true")
    parser.add_argument(
        "--use-local-csv",
        action="store_true",
        default=False,
        help="Compatibilidade legada; o modo atual usa historico direto da exchange.",
    )
    parser.add_argument(
        "--no-local-csv",
        dest="use_local_csv",
        action="store_false",
        help="Mantem o modo atual sem CSV local.",
    )
    args = parser.parse_args()

    run_backtest(
        args.symbol,
        args.timeframe,
        args.candles,
        args.fee_pct,
        testnet=args.testnet,
        use_local_csv=args.use_local_csv,
    )

