from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ia.dataset_builder import FEATURE_COLUMNS, prepare_feature_frame
from market_data import fetch_historical_candles, fetch_historical_candles_from_csv


def _load_interpreter(model_path: str | Path):
    try:
        from tflite_runtime.interpreter import Interpreter  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
        try:
            import tensorflow as tf  # type: ignore

            Interpreter = tf.lite.Interpreter
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Nenhum interpretador TensorFlow Lite disponivel. "
                "Instale tensorflow ou tflite_runtime na venv da IA."
            ) from exc

    interpreter = Interpreter(model_path=str(model_path))
    interpreter.allocate_tensors()
    return interpreter


class TFLiteSignalModel:
    def __init__(self, model_path: str | Path, metadata_path: str | Path):
        self.model_path = Path(model_path)
        self.metadata = json.loads(Path(metadata_path).read_text(encoding="utf-8"))
        self.feature_names = [str(item) for item in self.metadata["feature_names"]]
        self.label_names = [str(item) for item in self.metadata["label_names"]]
        self.interpreter = _load_interpreter(self.model_path)
        self.input_details = self.interpreter.get_input_details()[0]
        self.output_details = self.interpreter.get_output_details()[0]

    def predict_raw(self, feature_vector: np.ndarray) -> dict[str, Any]:
        model_input = np.asarray(feature_vector, dtype=np.float32).reshape(1, -1)
        self.interpreter.set_tensor(self.input_details["index"], model_input)
        self.interpreter.invoke()
        scores = self.interpreter.get_tensor(self.output_details["index"])[0].astype(float)
        best_index = int(np.argmax(scores))
        return {
            "label": self.label_names[best_index],
            "label_index": best_index,
            "scores": {
                self.label_names[index]: float(score)
                for index, score in enumerate(scores)
            },
        }

    def predict_latest(
        self,
        symbol: str,
        timeframe: str,
        *,
        total_limit: int = 2000,
        use_local_csv: bool = True,
        testnet: bool = False,
    ) -> dict[str, Any]:
        if use_local_csv:
            market_df = fetch_historical_candles_from_csv(symbol, timeframe, total_limit=total_limit)
        else:
            market_df = fetch_historical_candles(symbol, timeframe, total_limit=total_limit, testnet=testnet)

        feature_df = prepare_feature_frame(market_df)
        latest = feature_df.dropna(subset=self.feature_names).iloc[-1]
        vector = latest[self.feature_names].astype("float32").to_numpy()
        prediction = self.predict_raw(vector)
        prediction["symbol"] = symbol
        prediction["timeframe"] = timeframe
        prediction["timestamp"] = str(latest["timestamp"])
        prediction["feature_vector"] = {
            feature: float(latest[feature])
            for feature in self.feature_names
        }
        return prediction


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run TensorFlow Lite inference on the latest candle features.")
    parser.add_argument("--model", required=True)
    parser.add_argument("--metadata", required=True)
    parser.add_argument("--symbol", default="XLM/USDT")
    parser.add_argument("--timeframe", default="15m")
    parser.add_argument("--total-limit", type=int, default=2000)
    parser.add_argument("--use-exchange", action="store_true")
    parser.add_argument("--testnet", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    model = TFLiteSignalModel(args.model, args.metadata)
    result = model.predict_latest(
        args.symbol,
        args.timeframe,
        total_limit=args.total_limit,
        use_local_csv=not args.use_exchange,
        testnet=args.testnet,
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
