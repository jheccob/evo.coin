from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import shutil
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ia.dataset_builder import build_multi_symbol_dataset, save_dataset
from ia.train_tflite_model import train_and_export


def _utc_stamp() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _load_json(path: str | Path) -> dict:
    resolved = Path(path)
    if not resolved.exists():
        return {}
    try:
        payload = json.loads(resolved.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def evaluate_candidate(
    *,
    candidate_metadata: dict,
    current_metadata: dict | None = None,
    min_val_accuracy: float = 0.38,
    max_val_loss: float = 1.35,
    min_accuracy_delta: float = -0.03,
    min_action_precision: float = 0.20,
    min_validation_action_rows: int = 50,
) -> dict:
    candidate_metrics = dict(candidate_metadata.get("metrics") or {})
    current_metrics = dict((current_metadata or {}).get("metrics") or {})
    candidate_accuracy = _safe_float(candidate_metrics.get("val_accuracy"), 0.0)
    candidate_loss = _safe_float(candidate_metrics.get("val_loss"), 999.0)
    current_accuracy = _safe_float(current_metrics.get("val_accuracy"), 0.0)
    accuracy_delta = candidate_accuracy - current_accuracy
    action_precision = _calculate_action_precision(candidate_metadata)
    validation_action_rows = _calculate_validation_action_rows(candidate_metadata)

    reasons: list[str] = []
    approved = True
    if candidate_accuracy < float(min_val_accuracy):
        approved = False
        reasons.append(f"val_accuracy_below_floor:{candidate_accuracy:.4f}<{float(min_val_accuracy):.4f}")
    if candidate_loss > float(max_val_loss):
        approved = False
        reasons.append(f"val_loss_above_ceiling:{candidate_loss:.4f}>{float(max_val_loss):.4f}")
    if current_metrics and accuracy_delta < float(min_accuracy_delta):
        approved = False
        reasons.append(f"accuracy_delta_below_floor:{accuracy_delta:.4f}<{float(min_accuracy_delta):.4f}")
    if validation_action_rows < int(min_validation_action_rows):
        approved = False
        reasons.append(f"validation_action_rows_below_floor:{validation_action_rows}<{int(min_validation_action_rows)}")
    if action_precision < float(min_action_precision):
        approved = False
        reasons.append(f"action_precision_below_floor:{action_precision:.4f}<{float(min_action_precision):.4f}")

    if approved:
        reasons.append("candidate_passed_quality_gate")

    return {
        "approved": bool(approved),
        "reasons": reasons,
        "candidate_val_accuracy": round(candidate_accuracy, 6),
        "candidate_val_loss": round(candidate_loss, 6),
        "current_val_accuracy": round(current_accuracy, 6),
        "accuracy_delta": round(accuracy_delta, 6),
        "action_precision": round(action_precision, 6),
        "validation_action_rows": int(validation_action_rows),
        "min_val_accuracy": float(min_val_accuracy),
        "max_val_loss": float(max_val_loss),
        "min_accuracy_delta": float(min_accuracy_delta),
        "min_action_precision": float(min_action_precision),
        "min_validation_action_rows": int(min_validation_action_rows),
    }


def _calculate_validation_action_rows(candidate_metadata: dict) -> int:
    validation = dict(candidate_metadata.get("validation") or {})
    label_distribution = dict(validation.get("label_distribution") or {})
    return int(label_distribution.get("short", 0) or 0) + int(label_distribution.get("long", 0) or 0)


def _calculate_action_precision(candidate_metadata: dict) -> float:
    validation = dict(candidate_metadata.get("validation") or {})
    label_names = [str(item) for item in candidate_metadata.get("label_names") or []]
    confusion = validation.get("confusion_matrix") or []
    prediction_distribution = dict(validation.get("prediction_distribution") or {})
    if not label_names or not isinstance(confusion, list):
        return 0.0

    weighted_correct = 0
    weighted_predicted = 0
    for action in ("short", "long"):
        if action not in label_names:
            continue
        action_index = label_names.index(action)
        try:
            correct = int(confusion[action_index][action_index])
        except (IndexError, TypeError, ValueError):
            correct = 0
        predicted = int(prediction_distribution.get(action, 0) or 0)
        weighted_correct += correct
        weighted_predicted += predicted

    if weighted_predicted <= 0:
        return 0.0
    return float(weighted_correct / weighted_predicted)


def promote_candidate(
    *,
    candidate_dir: str | Path,
    runtime_model_path: str | Path,
    runtime_metadata_path: str | Path,
) -> dict:
    candidate_dir = Path(candidate_dir)
    source_model = candidate_dir / "model.tflite"
    source_metadata = candidate_dir / "metadata.json"
    if not source_model.exists() or not source_metadata.exists():
        raise FileNotFoundError("Candidato sem model.tflite ou metadata.json.")

    runtime_model_path = Path(runtime_model_path)
    runtime_metadata_path = Path(runtime_metadata_path)
    runtime_model_path.parent.mkdir(parents=True, exist_ok=True)
    runtime_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_model, runtime_model_path)
    shutil.copy2(source_metadata, runtime_metadata_path)
    return {
        "model_path": str(runtime_model_path),
        "metadata_path": str(runtime_metadata_path),
    }


def run_auto_retrain(
    *,
    symbols: list[str],
    timeframe: str,
    total_limit: int,
    label_mode: str,
    horizon_candles: int,
    target_pct: float,
    risk_buffer_pct: float,
    max_holding_candles: int,
    min_trade_net_pct: float,
    decision_edge_pct: float,
    sample_stride: int,
    output_root: str | Path,
    runtime_model_path: str | Path,
    runtime_metadata_path: str | Path,
    epochs: int,
    batch_size: int,
    validation_ratio: float,
    min_val_accuracy: float,
    max_val_loss: float,
    min_accuracy_delta: float,
    min_action_precision: float,
    min_validation_action_rows: int,
    promote: bool = False,
    use_exchange: bool = False,
) -> dict:
    run_id = _utc_stamp()
    output_root = Path(output_root)
    run_dir = output_root / f"auto_retrain_{run_id}"
    run_dir.mkdir(parents=True, exist_ok=True)
    dataset_path = run_dir / "dataset.npz"
    candidate_dir = run_dir / "candidate_model"

    sources = [
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "total_limit": int(total_limit),
            "use_local_csv": not bool(use_exchange),
            "testnet": False,
        }
        for symbol in symbols
    ]
    dataset = build_multi_symbol_dataset(
        sources,
        label_mode=label_mode,
        horizon_candles=int(horizon_candles),
        target_pct=float(target_pct),
        risk_buffer_pct=float(risk_buffer_pct),
        max_holding_candles=int(max_holding_candles),
        min_trade_net_pct=float(min_trade_net_pct),
        decision_edge_pct=float(decision_edge_pct),
        sample_stride=int(sample_stride),
    )
    save_dataset(dataset, dataset_path)

    train_result = train_and_export(
        dataset_path,
        candidate_dir,
        epochs=int(epochs),
        batch_size=int(batch_size),
        validation_ratio=float(validation_ratio),
        validation_symbol=symbols[0] if symbols else "",
    )
    candidate_metadata = _load_json(candidate_dir / "metadata.json")
    current_metadata = _load_json(runtime_metadata_path)
    gate = evaluate_candidate(
        candidate_metadata=candidate_metadata,
        current_metadata=current_metadata,
        min_val_accuracy=min_val_accuracy,
        max_val_loss=max_val_loss,
        min_accuracy_delta=min_accuracy_delta,
        min_action_precision=min_action_precision,
        min_validation_action_rows=min_validation_action_rows,
    )
    promotion = {"promoted": False}
    if promote and gate["approved"]:
        promotion = {
            "promoted": True,
            **promote_candidate(
                candidate_dir=candidate_dir,
                runtime_model_path=runtime_model_path,
                runtime_metadata_path=runtime_metadata_path,
            ),
        }

    report = {
        "run_id": run_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "symbols": symbols,
        "timeframe": timeframe,
        "label_mode": label_mode,
        "label_params": {
            "horizon_candles": int(horizon_candles),
            "target_pct": float(target_pct),
            "risk_buffer_pct": float(risk_buffer_pct),
            "max_holding_candles": int(max_holding_candles),
            "min_trade_net_pct": float(min_trade_net_pct),
            "decision_edge_pct": float(decision_edge_pct),
            "sample_stride": int(sample_stride),
        },
        "dataset_path": str(dataset_path),
        "candidate_dir": str(candidate_dir),
        "runtime_model_path": str(runtime_model_path),
        "runtime_metadata_path": str(runtime_metadata_path),
        "dataset_metadata": dataset.get("metadata") or {},
        "train_result": train_result,
        "quality_gate": gate,
        "promotion": promotion,
    }
    report_path = run_dir / "auto_retrain_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    latest_report_path = output_root / "latest_auto_retrain_report.json"
    latest_report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    report["report_path"] = str(report_path)
    report["latest_report_path"] = str(latest_report_path)
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Autonomous local retraining pipeline for the EVO AI model.")
    parser.add_argument("--symbols", default="BTC/USDT,XLM/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--total-limit", type=int, default=30000)
    parser.add_argument("--label-mode", default="trade_outcome")
    parser.add_argument("--horizon-candles", type=int, default=8)
    parser.add_argument("--target-pct", type=float, default=0.45)
    parser.add_argument("--risk-buffer-pct", type=float, default=0.30)
    parser.add_argument("--max-holding-candles", type=int, default=24)
    parser.add_argument("--min-trade-net-pct", type=float, default=0.12)
    parser.add_argument("--decision-edge-pct", type=float, default=0.08)
    parser.add_argument("--sample-stride", type=int, default=1)
    parser.add_argument("--output-root", default="ia/artifacts/auto_retrain")
    parser.add_argument("--runtime-model-path", default="data/models/runtime_model.tflite")
    parser.add_argument("--runtime-metadata-path", default="data/models/runtime_model_metadata.json")
    parser.add_argument("--epochs", type=int, default=24)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--min-val-accuracy", type=float, default=0.38)
    parser.add_argument("--max-val-loss", type=float, default=1.35)
    parser.add_argument("--min-accuracy-delta", type=float, default=-0.03)
    parser.add_argument("--min-action-precision", type=float, default=0.20)
    parser.add_argument("--min-validation-action-rows", type=int, default=50)
    parser.add_argument("--promote", action="store_true")
    parser.add_argument("--use-exchange", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = [item.strip() for item in str(args.symbols or "").split(",") if item.strip()]
    result = run_auto_retrain(
        symbols=symbols,
        timeframe=args.timeframe,
        total_limit=args.total_limit,
        label_mode=args.label_mode,
        horizon_candles=args.horizon_candles,
        target_pct=args.target_pct,
        risk_buffer_pct=args.risk_buffer_pct,
        max_holding_candles=args.max_holding_candles,
        min_trade_net_pct=args.min_trade_net_pct,
        decision_edge_pct=args.decision_edge_pct,
        sample_stride=args.sample_stride,
        output_root=args.output_root,
        runtime_model_path=args.runtime_model_path,
        runtime_metadata_path=args.runtime_metadata_path,
        epochs=args.epochs,
        batch_size=args.batch_size,
        validation_ratio=args.validation_ratio,
        min_val_accuracy=args.min_val_accuracy,
        max_val_loss=args.max_val_loss,
        min_accuracy_delta=args.min_accuracy_delta,
        min_action_precision=args.min_action_precision,
        min_validation_action_rows=args.min_validation_action_rows,
        promote=args.promote,
        use_exchange=args.use_exchange,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
