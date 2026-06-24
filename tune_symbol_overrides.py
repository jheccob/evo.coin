from __future__ import annotations

import argparse
import itertools
import json
import random
from datetime import UTC, datetime
from typing import Dict, List, Tuple

import config
from market_data import fetch_historical_candles, fetch_historical_candles_from_csv
from backtest import run_backtest
from strategy_engine import StrategyParams, calculate_indicators


SEARCH_SPACE = {
    "GLOBAL_MIN_ATR_PCT": [0.08, 0.10, 0.12, 0.15],
    "SHORT_MIN_ATR_PCT": [0.16, 0.20, 0.25],
    "MIN_TREND_STRENGTH_PCT": [0.08, 0.10, 0.12, 0.15],
    "MIN_TREND_STRENGTH_PCT_SHORT": [0.10, 0.14, 0.20],
    "LONG_ADX_THRESHOLD": [18, 21, 23],
    "SHORT_ADX_THRESHOLD": [16, 18, 20],
    "LONG_VOLUME_RATIO_REQUIRED": [1.2, 1.35, 1.5],
    "SHORT_VOLUME_RATIO_REQUIRED": [0.9, 1.0, 1.1],
    "MIN_LONG_SCORE": [6, 7, 8],
    "MIN_SHORT_SCORE": [6, 7, 8],
}


def _capture_base_values() -> Dict[str, object]:
    return {key: getattr(config, key) for key in config.SYMBOL_STRATEGY_OVERRIDE_KEYS if hasattr(config, key)}


def _apply_values(values: Dict[str, object]) -> None:
    for key, value in values.items():
        setattr(config, key, value)


def _summary_fields(summary: Dict[str, object]) -> Dict[str, float]:
    return {
        "trades": int(summary.get("trades", 0) or 0),
        "profit_factor": round(float(summary.get("profit_factor", 0.0) or 0.0), 4),
        "net_pct": round(float(summary.get("net_pct", 0.0) or 0.0), 4),
        "max_drawdown": round(float(summary.get("max_drawdown", 0.0) or 0.0), 4),
        "win_rate_pct": round(float(summary.get("win_rate_pct", 0.0) or 0.0), 4),
    }


def _candidate_score(summary_90: Dict[str, object], summary_180: Dict[str, object]) -> float:
    pf90 = float(summary_90.get("profit_factor", 0.0) or 0.0)
    pf180 = float(summary_180.get("profit_factor", 0.0) or 0.0)
    net90 = float(summary_90.get("net_pct", 0.0) or 0.0)
    net180 = float(summary_180.get("net_pct", 0.0) or 0.0)
    dd180 = float(summary_180.get("max_drawdown", 0.0) or 0.0)
    trades90 = float(summary_90.get("trades", 0) or 0)
    return (
        (net90 * 0.25)
        + (net180 * 0.75)
        + max(pf90 - 1.0, 0.0) * 14.0
        + max(pf180 - 1.0, 0.0) * 24.0
        + min(trades90, 90.0) * 0.02
        - dd180 * 0.35
    )


def _candidate_is_promising(summary_90: Dict[str, object], summary_180: Dict[str, object]) -> bool:
    return (
        float(summary_90.get("profit_factor", 0.0) or 0.0) >= 1.0
        and float(summary_90.get("net_pct", 0.0) or 0.0) > 0.0
        and int(summary_90.get("trades", 0) or 0) >= 20
        and float(summary_180.get("profit_factor", 0.0) or 0.0) >= 1.03
        and float(summary_180.get("net_pct", 0.0) or 0.0) > 0.0
        and float(summary_180.get("max_drawdown", 0.0) or 0.0) <= 30.0
    )


def _enumerate_candidates(max_trials: int, seed: int) -> List[Dict[str, object]]:
    keys = list(SEARCH_SPACE.keys())
    universe = []
    for values in itertools.product(*(SEARCH_SPACE[key] for key in keys)):
        universe.append({key: value for key, value in zip(keys, values)})
    rng = random.Random(seed)
    rng.shuffle(universe)
    return universe[: max(int(max_trials), 1)]


def _load_candles(symbol: str, timeframe: str, candles: int, use_local_csv: bool):
    if use_local_csv:
        try:
            return fetch_historical_candles_from_csv(symbol, timeframe, total_limit=candles)
        except FileNotFoundError:
            pass
    return fetch_historical_candles(symbol, timeframe, total_limit=candles, testnet=bool(config.BACKTEST_USE_TESTNET))


def _build_indicator_cache(symbol: str, timeframe: str, candles_map: List[int], use_local_csv: bool) -> Dict[int, object]:
    params = StrategyParams()
    cache: Dict[int, object] = {}
    for candles in sorted(set(int(value) for value in candles_map)):
        raw_df = _load_candles(symbol, timeframe, candles, use_local_csv=use_local_csv)
        cache[candles] = calculate_indicators(raw_df.reset_index(drop=True), params)
    return cache


def tune_symbol(
    symbol: str,
    timeframe: str,
    *,
    use_local_csv: bool,
    max_trials: int,
    seed: int,
    initial_balance: float,
    risk_per_trade_pct: float,
    short_candles: int,
    medium_candles: int,
    final_candles: int,
) -> Dict[str, object]:
    base_values = _capture_base_values()
    previous_overrides_path = config.SYMBOL_STRATEGY_OVERRIDES_PATH
    config.SYMBOL_STRATEGY_OVERRIDES_PATH = ""
    best: Dict[str, object] = {}
    indicator_cache = _build_indicator_cache(
        symbol=symbol,
        timeframe=timeframe,
        candles_map=[short_candles, medium_candles, final_candles],
        use_local_csv=use_local_csv,
    )

    try:
        for candidate in _enumerate_candidates(max_trials=max_trials, seed=seed):
            _apply_values(base_values)
            _apply_values(candidate)
            _, summary_90 = run_backtest(
                symbol=symbol,
                timeframe=timeframe,
                candles=short_candles,
                fee_pct=config.FEE_PCT,
                verbose=False,
                save_report=False,
                use_local_csv=False,
                preloaded_df=indicator_cache[short_candles],
                precomputed_indicators=True,
                initial_balance=initial_balance,
                risk_per_trade_pct=risk_per_trade_pct,
            )
            _, summary_180 = run_backtest(
                symbol=symbol,
                timeframe=timeframe,
                candles=medium_candles,
                fee_pct=config.FEE_PCT,
                verbose=False,
                save_report=False,
                use_local_csv=False,
                preloaded_df=indicator_cache[medium_candles],
                precomputed_indicators=True,
                initial_balance=initial_balance,
                risk_per_trade_pct=risk_per_trade_pct,
            )
            if not _candidate_is_promising(summary_90, summary_180):
                continue

            score = _candidate_score(summary_90, summary_180)
            if not best or score > float(best.get("score", float("-inf"))):
                best = {
                    "score": round(score, 6),
                    "overrides": dict(candidate),
                    "metrics": {
                        "short": _summary_fields(summary_90),
                        "medium": _summary_fields(summary_180),
                    },
                }

        if not best:
            return {
                "status": "no_candidate",
                "reason": "Nenhum conjunto sustentou PF/net nos recortes 90d e 180d.",
            }

        _apply_values(base_values)
        _apply_values(best["overrides"])
        _, summary_365 = run_backtest(
            symbol=symbol,
            timeframe=timeframe,
            candles=final_candles,
            fee_pct=config.FEE_PCT,
            verbose=False,
            save_report=False,
            use_local_csv=False,
            preloaded_df=indicator_cache[final_candles],
            precomputed_indicators=True,
            initial_balance=initial_balance,
            risk_per_trade_pct=risk_per_trade_pct,
        )
        best["metrics"]["final"] = _summary_fields(summary_365)

        pf365 = float(summary_365.get("profit_factor", 0.0) or 0.0)
        net365 = float(summary_365.get("net_pct", 0.0) or 0.0)
        dd365 = float(summary_365.get("max_drawdown", 0.0) or 0.0)
        if pf365 >= 1.02 and net365 > 0.0 and dd365 <= 35.0:
            best["status"] = "tuned"
            best["reason"] = "Candidato sustentou curto/medio prazo e validou na janela final."
        else:
            best["status"] = "watchlist"
            best["reason"] = "Candidato melhorou curto/medio prazo, mas janela final ainda fragil."
        return best
    finally:
        _apply_values(base_values)
        config.SYMBOL_STRATEGY_OVERRIDES_PATH = previous_overrides_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="*", default=["ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"])
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--use-local-csv", action="store_true", default=False)
    parser.add_argument("--max-trials", type=int, default=40)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--initial-balance", type=float, default=config.ProductionConfig.PAPER_ACCOUNT_BALANCE)
    parser.add_argument("--risk-per-trade-pct", type=float, default=config.RISK_PER_TRADE_PCT)
    parser.add_argument("--short-candles", type=int, default=2880)
    parser.add_argument("--medium-candles", type=int, default=8640)
    parser.add_argument("--final-candles", type=int, default=17280)
    parser.add_argument("--output", default=config.SYMBOL_STRATEGY_OVERRIDES_PATH)
    args = parser.parse_args()

    results: Dict[str, object] = {}
    for idx, symbol in enumerate(args.symbols):
        print(f"Tunando {symbol}...")
        result = tune_symbol(
            symbol=config.normalize_symbol(symbol),
            timeframe=str(args.timeframe),
            use_local_csv=bool(args.use_local_csv),
            max_trials=int(args.max_trials),
            seed=int(args.seed) + idx,
            initial_balance=float(args.initial_balance),
            risk_per_trade_pct=float(args.risk_per_trade_pct),
            short_candles=int(args.short_candles),
            medium_candles=int(args.medium_candles),
            final_candles=int(args.final_candles),
        )
        results[config.normalize_symbol(symbol)] = result
        print(f"  -> {result.get('status')}: {result.get('reason')}")

    payload = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "timeframe": str(args.timeframe),
        "search": {
            "max_trials": int(args.max_trials),
            "seed": int(args.seed),
            "use_local_csv": bool(args.use_local_csv),
            "initial_balance_usdt": float(args.initial_balance),
            "risk_per_trade_pct": float(args.risk_per_trade_pct),
            "short_candles": int(args.short_candles),
            "medium_candles": int(args.medium_candles),
            "final_candles": int(args.final_candles),
        },
        "symbols": results,
    }

    with open(str(args.output), "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
    print(f"Overrides salvos em {args.output}")


if __name__ == "__main__":
    main()
