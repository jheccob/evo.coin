from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

import config
from ia.dataset_builder import FEATURE_COLUMNS, prepare_feature_frame


LABEL_TO_SIGNAL = {
    "short": "sell",
    "hold": "hold",
    "long": "buy",
}


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _normalize_probabilities(probabilities: np.ndarray) -> np.ndarray:
    clipped = np.clip(np.asarray(probabilities, dtype="float32"), 1e-8, None)
    total = float(clipped.sum())
    if total <= 0:
        return np.array([0.0, 1.0, 0.0], dtype="float32")
    return clipped / total


def _extract_feature(feature_row, name: str, default: float = 0.0) -> float:
    if feature_row is None:
        return float(default)
    try:
        raw = feature_row.get(name, default)
    except Exception:
        raw = default
    try:
        if pd.isna(raw):
            return float(default)
    except Exception:
        pass
    return _safe_float(raw, default)


def _resolve_symbol_feature_value(symbol: str, feature_name: str) -> float | None:
    normalized_symbol = str(symbol or "").strip().upper()
    if feature_name == "source_is_btc":
        return 1.0 if normalized_symbol.startswith("BTC/") else 0.0
    if feature_name == "source_is_xlm":
        return 1.0 if normalized_symbol.startswith("XLM/") else 0.0
    return None


class _DisabledRuntimeModel:
    def __init__(self, reason: str = "AI runtime indisponivel neste workspace."):
        self.model_loaded = False
        self.metadata = {"model_version": "disabled-local-compat"}
        self.reason = reason

    def predict(self, feature_vector: np.ndarray) -> np.ndarray:
        del feature_vector
        return np.array([0.0, 1.0, 0.0], dtype="float32")


class RuntimeTFLiteModel:
    def __init__(self, model_path: str | Path, metadata_path: str | Path):
        self.model_path = Path(model_path)
        self.metadata_path = Path(metadata_path)
        self.model_loaded = False
        self.metadata: dict = {
            "model_version": self.model_path.name,
            "label_names": ["short", "hold", "long"],
            "feature_names": list(FEATURE_COLUMNS),
        }
        self.reason = ""
        self._interpreter = None
        self._input_index = None
        self._output_index = None

        try:
            import tensorflow as tf  # type: ignore

            metadata_payload = {}
            if self.metadata_path.exists():
                metadata_payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            self.metadata = {
                **self.metadata,
                **metadata_payload,
            }
            if not self.model_path.exists():
                raise FileNotFoundError(f"Modelo TFLite nao encontrado: {self.model_path}")

            self._interpreter = tf.lite.Interpreter(model_path=str(self.model_path))
            self._interpreter.allocate_tensors()
            input_details = self._interpreter.get_input_details()
            output_details = self._interpreter.get_output_details()
            self._input_index = int(input_details[0]["index"])
            self._output_index = int(output_details[0]["index"])
            self.model_loaded = True
        except Exception as exc:
            self.reason = str(exc)
            self._interpreter = None
            self.model_loaded = False

    @property
    def feature_names(self) -> list[str]:
        feature_names = self.metadata.get("feature_names") or FEATURE_COLUMNS
        return [str(item) for item in feature_names]

    @property
    def label_names(self) -> list[str]:
        label_names = self.metadata.get("label_names") or ["short", "hold", "long"]
        return [str(item) for item in label_names]

    def predict(self, feature_vector: np.ndarray) -> np.ndarray:
        if not self.model_loaded or self._interpreter is None or self._input_index is None or self._output_index is None:
            return np.array([0.0, 1.0, 0.0], dtype="float32")
        prepared = np.asarray(feature_vector, dtype="float32").reshape(1, -1)
        self._interpreter.set_tensor(self._input_index, prepared)
        self._interpreter.invoke()
        output = self._interpreter.get_tensor(self._output_index)
        return _normalize_probabilities(np.asarray(output[0], dtype="float32"))


class AIModel:
    def __init__(self):
        runtime_model = RuntimeTFLiteModel(
            getattr(config.ProductionConfig, "AI_MODEL_PATH"),
            getattr(config.ProductionConfig, "AI_MODEL_METADATA_PATH"),
        )
        if runtime_model.model_loaded:
            self.runtime_model = runtime_model
        else:
            self.runtime_model = _DisabledRuntimeModel(reason=runtime_model.reason or "Modelo TFLite indisponivel.")

    def get_runtime_status(self):
        dataset_metadata = (self.runtime_model.metadata or {}).get("dataset_metadata") or {}
        metrics = (self.runtime_model.metadata or {}).get("metrics") or {}
        return {
            "enabled": bool(getattr(config.ProductionConfig, "ENABLE_AI_ASSISTANT", False)),
            "runtime_loaded": bool(self.runtime_model.model_loaded),
            "model_version": self.runtime_model.metadata.get("model_version"),
            "runtime_version": self.runtime_model.metadata.get("model_version"),
            "dataset_rows": int(dataset_metadata.get("rows", 0) or 0),
            "metrics": metrics,
            "label_names": list(getattr(self.runtime_model, "label_names", ["short", "hold", "long"])),
            "feature_count": len(getattr(self.runtime_model, "feature_names", FEATURE_COLUMNS)),
            "reason": getattr(self.runtime_model, "reason", ""),
        }

    def score_candle_slice(self, candle_slice, *, symbol: str, timeframe: str, market_context: dict | None = None) -> dict:
        if not self.runtime_model.model_loaded:
            return {
                "enabled": False,
                "signal": "hold",
                "label": "hold",
                "confidence": 0.0,
                "reason": getattr(self.runtime_model, "reason", "ai_model_unavailable"),
                "probabilities": {"short": 0.0, "hold": 1.0, "long": 0.0},
                "context": market_context or {},
            }

        feature_frame = prepare_feature_frame(candle_slice.copy())
        if feature_frame.empty:
            return {
                "enabled": True,
                "signal": "hold",
                "label": "hold",
                "confidence": 0.0,
                "reason": "ai_feature_frame_empty",
                "probabilities": {"short": 0.0, "hold": 1.0, "long": 0.0},
                "context": market_context or {},
            }

        return self.score_feature_row(
            feature_frame.iloc[-1],
            symbol=symbol,
            timeframe=timeframe,
            market_context=market_context,
        )

    def score_feature_row(self, feature_row, *, symbol: str, timeframe: str, market_context: dict | None = None) -> dict:
        if not self.runtime_model.model_loaded:
            return {
                "enabled": False,
                "signal": "hold",
                "label": "hold",
                "confidence": 0.0,
                "reason": getattr(self.runtime_model, "reason", "ai_model_unavailable"),
                "probabilities": {"short": 0.0, "hold": 1.0, "long": 0.0},
                "context": market_context or {},
            }

        feature_names = list(self.runtime_model.feature_names)
        feature_values = []
        for feature_name in feature_names:
            source_feature_value = _resolve_symbol_feature_value(symbol, feature_name)
            if source_feature_value is not None:
                raw_value = source_feature_value
            else:
                raw_value = feature_row.get(feature_name, 0.0)
            try:
                is_missing = bool(pd.isna(raw_value))
            except Exception:
                is_missing = False
            if raw_value is None or is_missing:
                raw_value = 0.0
            feature_values.append(_safe_float(raw_value, 0.0))

        feature_vector = np.asarray(feature_values, dtype="float32")
        base_probabilities = self.runtime_model.predict(feature_vector)
        adjusted_probabilities = self._apply_market_context_bias(base_probabilities, market_context)

        label_names = self.runtime_model.label_names
        predicted_index = int(np.argmax(adjusted_probabilities))
        predicted_label = label_names[predicted_index]
        confidence = float(adjusted_probabilities[predicted_index])
        probability_map = {
            label_names[index]: round(float(adjusted_probabilities[index]), 6)
            for index in range(len(label_names))
        }

        return {
            "enabled": True,
            "symbol": symbol,
            "timeframe": timeframe,
            "signal": LABEL_TO_SIGNAL.get(predicted_label, "hold"),
            "label": predicted_label,
            "confidence": round(confidence, 6),
            "reason": f"ai_model:{self.runtime_model.metadata.get('model_version')}",
            "probabilities": probability_map,
            "raw_probabilities": {
                label_names[index]: round(float(base_probabilities[index]), 6)
                for index in range(len(label_names))
            },
            "context": market_context or {},
        }

    def should_exit_position(
        self,
        *,
        position: dict | None,
        ai_decision: dict | None,
        min_confidence: float,
        feature_row=None,
        market_context: dict | None = None,
        learning_bias: float = 0.0,
    ) -> dict:
        if not position or not ai_decision:
            return {"exit": False}

        confidence = _safe_float(ai_decision.get("confidence"), 0.0)
        if confidence < float(min_confidence):
            return {"exit": False, "monitor_reason": "ai_confidence_below_exit_threshold"}

        side = str(position.get("side") or "").strip().lower()
        signal = str(ai_decision.get("signal") or "").strip().lower()
        current_price = _extract_feature(feature_row, "close", _safe_float(position.get("entry_price"), 0.0))
        entry_price = _safe_float(position.get("entry_price"), 0.0)
        if entry_price <= 0 or current_price <= 0:
            return {"exit": False, "monitor_reason": "position_price_unavailable"}

        if side == "long":
            unrealized_pct = ((current_price - entry_price) / entry_price) * 100.0
        else:
            unrealized_pct = ((entry_price - current_price) / entry_price) * 100.0

        range_regime_score = _extract_feature(feature_row, "range_regime_score", 0.0)
        trend_regime_score = _extract_feature(feature_row, "trend_regime_score", 0.0)
        channel_position_32 = _extract_feature(feature_row, "channel_position_32", 0.5)
        resistance_gap_pct = _extract_feature(feature_row, "distance_to_rolling_high_pct", 999.0)
        support_gap_pct = _extract_feature(feature_row, "distance_to_rolling_low_pct", 999.0)
        resistance_pressure = _extract_feature(feature_row, "resistance_pressure_score", 0.0)
        support_pressure = _extract_feature(feature_row, "support_pressure_score", 0.0)
        rsi = _extract_feature(feature_row, "rsi", 50.0)
        rsi_delta = _extract_feature(feature_row, "rsi_delta", 0.0)
        adx_delta = _extract_feature(feature_row, "adx_delta", 0.0)
        ai_probs = ai_decision.get("probabilities") or {}
        long_prob = _safe_float(ai_probs.get("long"), 0.0)
        short_prob = _safe_float(ai_probs.get("short"), 0.0)
        caution_bias = _safe_float(((market_context or {}).get("bias") or {}).get("caution_bias"), 0.0)
        near_level_pct = float(getattr(config.ProductionConfig, "AI_STRUCTURE_EXIT_NEAR_LEVEL_PCT", 0.35) or 0.35)
        range_threshold = float(getattr(config.ProductionConfig, "AI_STRUCTURE_RANGE_THRESHOLD", 0.55) or 0.55)
        trend_weak_threshold = float(
            getattr(config.ProductionConfig, "AI_STRUCTURE_TREND_WEAK_THRESHOLD", 0.35) or 0.35
        )
        min_profit_pct = float(getattr(config.ProductionConfig, "AI_STRUCTURE_EXIT_MIN_PROFIT_PCT", 0.35) or 0.35)
        require_protection = bool(getattr(config.ProductionConfig, "AI_STRUCTURE_EXIT_REQUIRE_PROTECTION", True))
        strong_confidence_bonus = float(
            getattr(config.ProductionConfig, "AI_STRUCTURE_EXIT_STRONG_CONFIDENCE_BONUS", 0.08) or 0.08
        )
        break_even_active = bool(position.get("break_even_active", False))
        partial_taken = bool(position.get("partial_taken", False))
        trailing_trigger_pct = _safe_float(position.get("trailing_trigger_pct"), 0.0)
        protection_active = break_even_active or partial_taken
        profit_ready = unrealized_pct >= max(
            min_profit_pct,
            (trailing_trigger_pct * 0.8) if trailing_trigger_pct > 0 else min_profit_pct,
        )

        if side == "long" and signal == "sell":
            return {
                "exit": True,
                "reason": f"ai_exit_reverse_short_conf_{confidence:.2f}",
                "confidence": confidence,
            }
        if side == "short" and signal == "buy":
            return {
                "exit": True,
                "reason": f"ai_exit_reverse_long_conf_{confidence:.2f}",
                "confidence": confidence,
            }

        if require_protection and not protection_active and not profit_ready:
            return {
                "exit": False,
                "monitor_reason": "structure_exit_waiting_for_protection",
            }

        degraded_learning = float(learning_bias) <= -0.05
        structure_meta = {
            "current_price": round(current_price, 8),
            "unrealized_pct": round(unrealized_pct, 6),
            "range_regime_score": round(range_regime_score, 6),
            "trend_regime_score": round(trend_regime_score, 6),
            "channel_position_32": round(channel_position_32, 6),
            "resistance_gap_pct": round(resistance_gap_pct, 6),
            "support_gap_pct": round(support_gap_pct, 6),
            "resistance_pressure_score": round(resistance_pressure, 6),
            "support_pressure_score": round(support_pressure, 6),
            "learning_bias": round(float(learning_bias), 6),
            "caution_bias": round(caution_bias, 6),
        }

        if side == "long":
            soft_exit = (
                unrealized_pct >= min_profit_pct
                and resistance_gap_pct <= near_level_pct
                and resistance_pressure >= 0.82
                and range_regime_score >= (range_threshold - (0.05 if degraded_learning else 0.0))
                and confidence >= (float(min_confidence) + strong_confidence_bonus)
                and (rsi >= 62.0 or rsi_delta <= -1.0 or adx_delta <= -2.0 or short_prob >= (long_prob + 0.05))
            )
            trend_fade_exit = (
                unrealized_pct >= (min_profit_pct + 0.50)
                and trend_regime_score <= trend_weak_threshold
                and channel_position_32 >= 0.82
                and confidence >= (float(min_confidence) + strong_confidence_bonus)
                and (rsi_delta <= -1.0 or adx_delta <= -2.0 or caution_bias >= 0.08)
            )
            if soft_exit:
                return {
                    "exit": True,
                    "reason": f"ai_exit_long_resistance_range_conf_{confidence:.2f}",
                    "confidence": confidence,
                    "structure": structure_meta,
                }
            if trend_fade_exit:
                return {
                    "exit": True,
                    "reason": f"ai_exit_long_trend_fade_conf_{confidence:.2f}",
                    "confidence": confidence,
                    "structure": structure_meta,
                }
            return {
                "exit": False,
                "monitor_reason": "hold_long_structure_ok",
                "structure": structure_meta,
            }

        soft_exit = (
            unrealized_pct >= min_profit_pct
            and support_gap_pct <= near_level_pct
            and support_pressure >= 0.82
            and range_regime_score >= (range_threshold - (0.05 if degraded_learning else 0.0))
            and confidence >= (float(min_confidence) + strong_confidence_bonus)
            and (rsi <= 38.0 or rsi_delta >= 1.0 or adx_delta <= -2.0 or long_prob >= (short_prob + 0.05))
        )
        trend_fade_exit = (
            unrealized_pct >= (min_profit_pct + 0.50)
            and trend_regime_score <= trend_weak_threshold
            and channel_position_32 <= 0.18
            and confidence >= (float(min_confidence) + strong_confidence_bonus)
            and (rsi_delta >= 1.0 or adx_delta <= -2.0 or caution_bias >= 0.08)
        )
        if soft_exit:
            return {
                "exit": True,
                "reason": f"ai_exit_short_support_range_conf_{confidence:.2f}",
                "confidence": confidence,
                "structure": structure_meta,
            }
        if trend_fade_exit:
            return {
                "exit": True,
                "reason": f"ai_exit_short_trend_fade_conf_{confidence:.2f}",
                "confidence": confidence,
                "structure": structure_meta,
            }
        return {
            "exit": False,
            "monitor_reason": "hold_short_structure_ok",
            "structure": structure_meta,
        }

    def _apply_market_context_bias(self, base_probabilities: np.ndarray, market_context: dict | None) -> np.ndarray:
        probabilities = np.asarray(base_probabilities, dtype="float32").copy()
        if not market_context:
            return _normalize_probabilities(probabilities)

        label_names = self.runtime_model.label_names
        label_to_index = {label: index for index, label in enumerate(label_names)}
        long_index = label_to_index.get("long")
        short_index = label_to_index.get("short")
        hold_index = label_to_index.get("hold")
        bias = (market_context.get("bias") or {}) if isinstance(market_context, dict) else {}
        long_bias = _safe_float(bias.get("long_bias"), 0.0)
        short_bias = _safe_float(bias.get("short_bias"), 0.0)
        caution_bias = _safe_float(bias.get("caution_bias"), 0.0)

        if long_index is not None:
            probabilities[long_index] += long_bias
        if short_index is not None:
            probabilities[short_index] += short_bias
        if hold_index is not None:
            probabilities[hold_index] += caution_bias

        return _normalize_probabilities(probabilities)
