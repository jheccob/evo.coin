from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _import_tensorflow():
    try:
        import tensorflow as tf  # type: ignore

        return tf
    except ImportError as exc:  # pragma: no cover - depends on local TensorFlow install
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        raise RuntimeError(
            "TensorFlow nao esta disponivel neste interpretador. "
            f"Python atual: {python_version}. "
            "Para a pasta IA, crie uma venv separada em Python 3.11/3.12 e instale "
            "os requisitos de ia\\requirements-tflite.txt."
        ) from exc


def load_dataset(path: str | Path) -> tuple[np.ndarray, np.ndarray, list[str], list[str], dict]:
    with np.load(Path(path), allow_pickle=False) as payload:
        metadata = json.loads(str(payload["metadata_json"]))
        feature_names = [str(item) for item in payload["feature_names"].tolist()]
        label_names = [str(item) for item in payload["label_names"].tolist()]
        symbols = [str(item) for item in payload["symbols"].tolist()] if "symbols" in payload.files else []
        timeframes = [str(item) for item in payload["timeframes"].tolist()] if "timeframes" in payload.files else []
        return (
            payload["features"].astype("float32"),
            payload["labels"].astype("int32"),
            feature_names,
            label_names,
            metadata,
            np.asarray(symbols),
            np.asarray(timeframes),
        )


def build_model(tf, input_width: int, class_count: int):
    normalizer = tf.keras.layers.Normalization(axis=-1)
    model = tf.keras.Sequential(
        [
            tf.keras.layers.Input(shape=(input_width,), name="signal_features"),
            normalizer,
            tf.keras.layers.Dense(128, activation="relu"),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Dropout(0.20),
            tf.keras.layers.Dense(64, activation="relu"),
            tf.keras.layers.BatchNormalization(),
            tf.keras.layers.Dropout(0.15),
            tf.keras.layers.Dense(32, activation="relu"),
            tf.keras.layers.Dense(class_count, activation="softmax", name="signal_probs"),
        ]
    )
    model.compile(
        optimizer=tf.keras.optimizers.Adam(learning_rate=0.001),
        loss="sparse_categorical_crossentropy",
        metrics=["accuracy"],
    )
    return model, normalizer


def _downsample_hold_rows(
    x_train: np.ndarray,
    y_train: np.ndarray,
    *,
    max_hold_ratio: float,
    random_seed: int,
) -> tuple[np.ndarray, np.ndarray, dict | None]:
    if float(max_hold_ratio) <= 0.0:
        return x_train, y_train, None

    hold_indexes = np.where(y_train == 1)[0]
    action_indexes = np.where(y_train != 1)[0]
    if len(hold_indexes) == 0 or len(action_indexes) == 0:
        return x_train, y_train, None

    max_hold_rows = int(max(len(action_indexes) * float(max_hold_ratio), 1))
    if len(hold_indexes) <= max_hold_rows:
        return x_train, y_train, None

    rng = np.random.default_rng(int(random_seed))
    sampled_hold_indexes = np.sort(rng.choice(hold_indexes, size=max_hold_rows, replace=False))
    selected_indexes = np.sort(np.concatenate([action_indexes, sampled_hold_indexes]))
    summary = {
        "enabled": True,
        "random_seed": int(random_seed),
        "max_hold_ratio": float(max_hold_ratio),
        "original_rows": int(len(y_train)),
        "original_hold_rows": int(len(hold_indexes)),
        "original_action_rows": int(len(action_indexes)),
        "balanced_rows": int(len(selected_indexes)),
        "balanced_hold_rows": int(len(sampled_hold_indexes)),
        "balanced_action_rows": int(len(action_indexes)),
    }
    return x_train[selected_indexes], y_train[selected_indexes], summary


def _build_validation_mask(
    labels: np.ndarray,
    symbols: np.ndarray,
    *,
    validation_ratio: float,
    validation_symbol: str,
    validation_strategy: str,
) -> np.ndarray:
    row_count = int(len(labels))
    val_mask = np.zeros(row_count, dtype=bool)
    if row_count == 0:
        return val_mask

    resolved_ratio = min(max(float(validation_ratio), 0.01), 0.50)
    resolved_symbol = str(validation_symbol or "").strip()
    resolved_strategy = str(validation_strategy or "per_symbol_tail").strip().lower()

    if len(symbols) == row_count and resolved_strategy == "per_symbol_tail":
        unique_symbols = [str(item) for item in np.unique(symbols) if str(item).strip()]
        for symbol in unique_symbols:
            symbol_indexes = np.where(symbols == symbol)[0]
            if len(symbol_indexes) == 0:
                continue
            validation_count = max(int(len(symbol_indexes) * resolved_ratio), 1)
            val_mask[symbol_indexes[-validation_count:]] = True
        if val_mask.any():
            return val_mask

    if len(symbols) == row_count and resolved_symbol:
        symbol_indexes = np.where(symbols == resolved_symbol)[0]
        if len(symbol_indexes) > 1:
            validation_count = max(int(len(symbol_indexes) * resolved_ratio), 1)
            val_mask[symbol_indexes[-validation_count:]] = True
            return val_mask

    split_index = max(int(row_count * (1.0 - resolved_ratio)), 1)
    val_mask[split_index:] = True
    return val_mask


def train_and_export(
    dataset_path: str | Path,
    output_dir: str | Path,
    *,
    epochs: int = 24,
    batch_size: int = 64,
    validation_ratio: float = 0.2,
    validation_symbol: str = "XLM/USDT",
    validation_strategy: str = "per_symbol_tail",
    class_weight_power: float = 1.0,
    max_hold_ratio: float = 6.0,
    random_seed: int = 42,
) -> dict:
    tf = _import_tensorflow()
    features, labels, feature_names, label_names, metadata, symbols, timeframes = load_dataset(dataset_path)
    if len(features) < 100:
        raise ValueError("Dataset pequeno demais para treinar com seguranca. Gere mais exemplos antes de exportar.")

    val_mask = _build_validation_mask(
        labels,
        symbols,
        validation_ratio=validation_ratio,
        validation_symbol=validation_symbol,
        validation_strategy=validation_strategy,
    )

    train_mask = ~val_mask
    x_train, x_val = features[train_mask], features[val_mask]
    y_train, y_val = labels[train_mask], labels[val_mask]
    if len(x_val) == 0:
        raise ValueError("O dataset precisa de mais exemplos para separar treino e validacao.")

    x_train, y_train, balance_summary = _downsample_hold_rows(
        x_train,
        y_train,
        max_hold_ratio=max_hold_ratio,
        random_seed=random_seed,
    )

    model, normalizer = build_model(tf, input_width=x_train.shape[1], class_count=len(label_names))
    normalizer.adapt(x_train)

    unique_labels, counts = np.unique(y_train, return_counts=True)
    raw_class_weights = {
        int(label): float(len(y_train) / (len(unique_labels) * count))
        for label, count in zip(unique_labels, counts)
        if count > 0
    }
    if float(class_weight_power) <= 0.0:
        class_weights = None
    else:
        class_weights = {
            label: float(weight ** float(class_weight_power))
            for label, weight in raw_class_weights.items()
        }

    callbacks = [
        tf.keras.callbacks.EarlyStopping(
            monitor="val_loss",
            patience=6,
            restore_best_weights=True,
        ),
        tf.keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss",
            factor=0.5,
            patience=3,
            min_lr=1e-5,
        ),
    ]

    history = model.fit(
        x_train,
        y_train,
        validation_data=(x_val, y_val),
        epochs=epochs,
        batch_size=batch_size,
        verbose=0,
        class_weight=class_weights,
        callbacks=callbacks,
    )
    evaluation = model.evaluate(x_val, y_val, verbose=0, return_dict=True)
    val_probs = model.predict(x_val, verbose=0)
    val_predictions = val_probs.argmax(axis=1)
    confusion = np.zeros((len(label_names), len(label_names)), dtype=int)
    for true_label, predicted_label in zip(y_val, val_predictions):
        confusion[int(true_label), int(predicted_label)] += 1

    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    keras_model_path = output_path / "model.keras"
    model.save(keras_model_path)

    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    tflite_model = converter.convert()

    tflite_path = output_path / "model.tflite"
    tflite_path.write_bytes(tflite_model)

    metadata_payload = {
        "source_dataset": str(Path(dataset_path)),
        "feature_names": feature_names,
        "label_names": label_names,
        "dataset_metadata": metadata,
        "metrics": {
            "val_loss": float(evaluation["loss"]),
            "val_accuracy": float(evaluation["accuracy"]),
            "epochs_requested": int(epochs),
            "epochs_completed": int(len(history.history.get("loss", []))),
            "batch_size": int(batch_size),
            "validation_ratio": float(validation_ratio),
            "validation_symbol": validation_symbol,
            "validation_strategy": validation_strategy,
            "max_hold_ratio": float(max_hold_ratio),
            "random_seed": int(random_seed),
            "train_label_distribution": {
                label_names[index]: int((y_train == index).sum())
                for index in range(len(label_names))
            },
        },
        "class_weights": class_weights or {},
        "raw_class_weights": raw_class_weights,
        "train_balancing": balance_summary or {"enabled": False},
        "validation": {
            "rows": int(len(y_val)),
            "symbols": {
                str(symbol): int(count)
                for symbol, count in zip(*np.unique(symbols[val_mask], return_counts=True))
            } if len(symbols) == len(labels) else {},
            "label_distribution": {
                label_names[index]: int((y_val == index).sum())
                for index in range(len(label_names))
            },
            "prediction_distribution": {
                label_names[index]: int((val_predictions == index).sum())
                for index in range(len(label_names))
            },
            "confusion_matrix": confusion.tolist(),
        },
    }
    metadata_path = output_path / "metadata.json"
    metadata_path.write_text(json.dumps(metadata_payload, ensure_ascii=True, indent=2), encoding="utf-8")

    history_path = output_path / "history.json"
    history_payload = {key: [float(item) for item in values] for key, values in history.history.items()}
    history_path.write_text(json.dumps(history_payload, ensure_ascii=True, indent=2), encoding="utf-8")

    return {
        "model_path": str(tflite_path),
        "keras_model_path": str(keras_model_path),
        "metadata_path": str(metadata_path),
        "history_path": str(history_path),
        "metrics": metadata_payload["metrics"],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Train a small dense network and export it as TensorFlow Lite.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--epochs", type=int, default=24)
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--validation-ratio", type=float, default=0.2)
    parser.add_argument("--validation-symbol", default="XLM/USDT")
    parser.add_argument("--validation-strategy", default="per_symbol_tail")
    parser.add_argument("--class-weight-power", type=float, default=1.0)
    parser.add_argument("--max-hold-ratio", type=float, default=6.0)
    parser.add_argument("--random-seed", type=int, default=42)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    result = train_and_export(
        args.dataset,
        args.output_dir,
        epochs=args.epochs,
        batch_size=args.batch_size,
        validation_ratio=args.validation_ratio,
        validation_symbol=args.validation_symbol,
        validation_strategy=args.validation_strategy,
        class_weight_power=args.class_weight_power,
        max_hold_ratio=args.max_hold_ratio,
        random_seed=args.random_seed,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
