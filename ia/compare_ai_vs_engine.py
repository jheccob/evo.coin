from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import tensorflow as tf

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import config
from ia.dataset_builder import LABEL_NAMES, build_supervised_dataset
from strategy_engine import StrategyParams, generate_entry_signal


def _map_engine_signal(signal_payload: dict[str, Any]) -> int:
    signal = str(signal_payload.get("signal") or "").strip().lower()
    if signal == "buy":
        return 2
    if signal == "sell":
        return 0
    return 1


def _summarize_predictions(name: str, labels: np.ndarray, predictions: np.ndarray) -> dict[str, Any]:
    actionable_mask = predictions != 1
    long_mask = predictions == 2
    short_mask = predictions == 0
    total = int(len(labels))
    actionable_count = int(actionable_mask.sum())
    action_correct = int(((predictions == labels) & actionable_mask).sum())
    overall_correct = int((predictions == labels).sum())

    return {
        "name": name,
        "total_rows": total,
        "overall_accuracy_pct": round(overall_correct / total * 100, 2) if total else 0.0,
        "actionable_signals": actionable_count,
        "actionable_rate_pct": round(actionable_count / total * 100, 2) if total else 0.0,
        "actionable_precision_pct": round(action_correct / actionable_count * 100, 2) if actionable_count else 0.0,
        "long_signals": int(long_mask.sum()),
        "long_precision_pct": round((((labels == 2) & long_mask).sum() / long_mask.sum()) * 100, 2) if long_mask.sum() else 0.0,
        "short_signals": int(short_mask.sum()),
        "short_precision_pct": round((((labels == 0) & short_mask).sum() / short_mask.sum()) * 100, 2) if short_mask.sum() else 0.0,
        # Proxy simples: acerto paga target, erro paga buffer de risco.
        "proxy_expectancy_pct": round(
            ((((predictions == labels) & actionable_mask).sum() * 0.45) - (((predictions != labels) & actionable_mask).sum() * 0.30))
            / actionable_count,
            4,
        ) if actionable_count else 0.0,
    }


def _build_engine_report(frame, validation_start_index: int, validation_labels: np.ndarray) -> tuple[np.ndarray, list[dict[str, Any]], list[list[Any]]]:
    params = StrategyParams()
    predictions = []
    actionable_rows: list[dict[str, Any]] = []
    hold_reasons: dict[str, int] = {}

    for index in range(validation_start_index, len(frame)):
        signal_payload = generate_entry_signal(frame, params, index=index)
        mapped_prediction = _map_engine_signal(signal_payload)
        predictions.append(mapped_prediction)

        if mapped_prediction == 1:
            reason = str(signal_payload.get("reason") or "")
            hold_reasons[reason] = hold_reasons.get(reason, 0) + 1
            continue

        side = "long" if mapped_prediction == 2 else "short"
        actionable_rows.append(
            {
                "timestamp": str(frame.iloc[index]["timestamp"]),
                "side": side,
                "setup": str(signal_payload.get("setup_name") or ""),
                "reason": str(signal_payload.get("reason") or ""),
                "true_label": LABEL_NAMES[int(validation_labels[index - validation_start_index])],
            }
        )

    top_hold_reasons = sorted(hold_reasons.items(), key=lambda item: item[1], reverse=True)[:12]
    return np.asarray(predictions, dtype=np.int32), actionable_rows, top_hold_reasons


def compare_ai_vs_engine(
    *,
    symbol: str,
    timeframe: str,
    total_limit: int,
    model_path: str | Path,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    config.apply_symbol_strategy_overrides(symbol)
    dataset = build_supervised_dataset(symbol, timeframe, total_limit=total_limit, use_local_csv=True)
    frame = dataset["frame"].reset_index(drop=True)
    features = dataset["features"]
    labels = dataset["labels"]

    split_index = int(len(frame) * 0.8)
    validation_frame = frame.iloc[split_index:].reset_index(drop=True)
    validation_features = features[split_index:]
    validation_labels = labels[split_index:]

    model = tf.keras.models.load_model(model_path)
    probabilities = model.predict(validation_features, verbose=0)
    ai_predictions = probabilities.argmax(axis=1).astype(np.int32)

    engine_predictions, engine_actionables, top_hold_reasons = _build_engine_report(frame, split_index, validation_labels)

    report = {
        "symbol": symbol,
        "timeframe": timeframe,
        "dataset_rows": int(len(frame)),
        "validation_rows": int(len(validation_labels)),
        "validation_period_start": str(validation_frame.iloc[0]["timestamp"]),
        "validation_period_end": str(validation_frame.iloc[-1]["timestamp"]),
        "validation_label_counts": {
            LABEL_NAMES[label_index]: int((validation_labels == label_index).sum())
            for label_index in range(len(LABEL_NAMES))
        },
        "ai_predicted_class_counts": {
            LABEL_NAMES[label_index]: int((ai_predictions == label_index).sum())
            for label_index in range(len(LABEL_NAMES))
        },
        "ai_max_probability_stats": {
            "min": round(float(probabilities.max(axis=1).min()), 6),
            "p25": round(float(np.quantile(probabilities.max(axis=1), 0.25)), 6),
            "median": round(float(np.quantile(probabilities.max(axis=1), 0.50)), 6),
            "p75": round(float(np.quantile(probabilities.max(axis=1), 0.75)), 6),
            "max": round(float(probabilities.max(axis=1).max()), 6),
        },
        "ai_summary": _summarize_predictions("ai", validation_labels, ai_predictions),
        "engine_summary": _summarize_predictions("engine", validation_labels, engine_predictions),
        "engine_first_actionable_signals": engine_actionables[:12],
        "engine_top_hold_reasons": top_hold_reasons,
    }

    if output_path is not None:
        destination = Path(output_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare AI model predictions against the classical engine on the same validation slice.")
    parser.add_argument("--symbol", default="XLM/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--total-limit", type=int, default=30000)
    parser.add_argument("--model", default="ia/artifacts/xlm_15m_model/model.keras")
    parser.add_argument("--output", default="")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    report = compare_ai_vs_engine(
        symbol=args.symbol,
        timeframe=args.timeframe,
        total_limit=args.total_limit,
        model_path=args.model,
        output_path=(args.output or None),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
