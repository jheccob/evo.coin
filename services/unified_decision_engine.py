from __future__ import annotations

from dataclasses import asdict

import config
from ai_model import AIModel
from ia.dataset_builder import prepare_feature_frame
from services.adaptive_learning_service import AdaptiveLearningService, LearningBias
from services.market_context_service import MarketContextService
from strategy_engine import StrategyParams, calculate_indicators, detect_market_regime, generate_entry_signal


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _extract_feature(feature_row, key: str, default: float = 0.0) -> float:
    if feature_row is None:
        return float(default)
    if hasattr(feature_row, "get"):
        return _safe_float(feature_row.get(key), default)
    try:
        return _safe_float(feature_row[key], default)
    except Exception:
        return float(default)


def _signal_to_side(signal: str) -> str:
    return "long" if str(signal or "").strip().lower() == "buy" else "short"


def _opposite_signal(signal: str) -> str:
    return "sell" if str(signal or "").strip().lower() == "buy" else "buy"


def _resolve_assist_mode() -> str:
    return str(getattr(config.ProductionConfig, "AI_ASSIST_MODE", "hybrid") or "hybrid").strip().lower()


def _serialize_ai_decision(ai_decision: dict | None) -> dict:
    if not ai_decision:
        return {}
    context = ai_decision.get("context") or {}
    fear_greed = context.get("fear_greed") or {}
    news = context.get("news") or {}
    bias = context.get("bias") or {}
    return {
        "signal": ai_decision.get("signal"),
        "label": ai_decision.get("label"),
        "confidence": ai_decision.get("confidence"),
        "reason": ai_decision.get("reason"),
        "probabilities": ai_decision.get("probabilities") or {},
        "fear_greed_value": fear_greed.get("value"),
        "fear_greed_classification": fear_greed.get("classification"),
        "news_sentiment_score": news.get("sentiment_score"),
        "news_headline_count": news.get("headline_count"),
        "context_bias": bias,
    }


class UnifiedDecisionEngine:
    def __init__(
        self,
        *,
        symbol: str,
        timeframe: str,
        ai_model: AIModel | None = None,
        market_context_service: MarketContextService | None = None,
        learning_service: AdaptiveLearningService | None = None,
        database=None,
        use_live_context: bool = True,
    ):
        self.symbol = symbol
        self.timeframe = timeframe
        self.database = database
        self.use_live_context = bool(use_live_context)
        should_load_ai = ai_model is not None or bool(getattr(config.ProductionConfig, "ENABLE_AI_ASSISTANT", True)) or _resolve_assist_mode() == "market_reading"
        self.ai_model = ai_model or (AIModel() if should_load_ai else None)
        self.market_context_service = market_context_service or (MarketContextService() if should_load_ai else None)
        self.learning_service = learning_service or AdaptiveLearningService(
            getattr(config.ProductionConfig, "AI_MEMORY_PATH"),
            enabled=bool(getattr(config.ProductionConfig, "AI_ONLINE_LEARNING_ENABLED", True)),
            min_trades=int(getattr(config.ProductionConfig, "AI_MEMORY_MIN_TRADES", 6) or 6),
            max_bias=float(getattr(config.ProductionConfig, "AI_MEMORY_MAX_BIAS", 0.12) or 0.12),
            database=database,
            memory_key=f"{self.symbol}|{self.timeframe}",
        )

    def _build_market_reading_result(
        self,
        *,
        working_slice,
        params: StrategyParams,
        feature_row,
        ai_decision: dict,
        market_context: dict | None,
    ) -> dict:
        ai_signal = str(ai_decision.get("signal") or "hold").strip().lower()
        probabilities = ai_decision.get("probabilities") or {}
        hold_prob = _safe_float(probabilities.get("hold"), 0.0)
        long_prob = _safe_float(probabilities.get("long"), 0.0)
        short_prob = _safe_float(probabilities.get("short"), 0.0)
        directional_min_prob = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_DIRECTIONAL_MIN_PROB", 0.34) or 0.34
        )
        directional_edge = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_DIRECTIONAL_EDGE", 0.03) or 0.03
        )
        hold_edge = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_HOLD_EDGE", 0.06) or 0.06
        )
        if ai_signal == "hold":
            if (
                long_prob >= directional_min_prob
                and long_prob >= short_prob + directional_edge
                and long_prob >= hold_prob + hold_edge
            ):
                ai_signal = "buy"
            elif (
                short_prob >= directional_min_prob
                and short_prob >= long_prob + directional_edge
                and short_prob >= hold_prob + hold_edge
            ):
                ai_signal = "sell"
        confidence = _safe_float(
            ai_decision.get("confidence"),
            max(long_prob, short_prob) if ai_signal in {"buy", "sell"} else hold_prob,
        )

        if ai_signal not in {"buy", "sell"}:
            return {
                "signal": "hold",
                "reason": (
                    "market_reading_hold_ai_signal | "
                    f"long={long_prob:.2f} | short={short_prob:.2f} | hold={hold_prob:.2f}"
                ),
                "setup": {"setup": "market_reading_hold", "direction": None, "source": "ai_market_reading"},
                "score": 0.0,
                "atr": _extract_feature(feature_row, "atr", 0.0),
                "decision_source": "market_reading_hold",
                "ai_decision": _serialize_ai_decision(ai_decision),
            }

        min_confidence = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_MIN_CONFIDENCE", 0.42) or 0.42
        )
        if confidence < min_confidence:
            return {
                "signal": "hold",
                "reason": f"market_reading_confidence_below_floor | confidence={confidence:.2f}",
                "setup": {"setup": "market_reading_hold", "direction": None, "source": "ai_market_reading"},
                "score": round(confidence, 6),
                "atr": _extract_feature(feature_row, "atr", 0.0),
                "decision_source": "market_reading_hold",
                "ai_decision": _serialize_ai_decision(ai_decision),
            }

        side = _signal_to_side(ai_signal)
        setup_name = f"market_reading_{side}"
        ai_support = _safe_float(probabilities.get("long" if side == "long" else "short"), 0.0)
        ai_opposition = _safe_float(probabilities.get("short" if side == "long" else "long"), 0.0)
        action_margin = ai_support - max(hold_prob, ai_opposition)
        learning_bias = self.learning_service.get_bias(
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=side,
            setup_name=setup_name,
        )
        combined_score = ai_support + learning_bias.bias - (ai_opposition * 0.05)
        approval_threshold = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_APPROVAL_THRESHOLD", 0.38) or 0.38
        )
        min_action_margin = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_MIN_ACTION_MARGIN", 0.08) or 0.08
        )
        regime = detect_market_regime(working_slice, params)
        regime_name = str(regime.get("regime") or "range")
        trend_score = _extract_feature(feature_row, "trend_regime_score", 0.0)
        range_score = _extract_feature(feature_row, "range_regime_score", 0.0)
        adx_value = _extract_feature(feature_row, "adx", 0.0)
        channel_position = _extract_feature(feature_row, "channel_position_32", 0.5)
        distance_to_high = _extract_feature(feature_row, "distance_to_rolling_high_pct", 99.0)
        distance_to_low = _extract_feature(feature_row, "distance_to_rolling_low_pct", 99.0)
        resistance_pressure = _extract_feature(feature_row, "resistance_pressure_score", 0.0)
        support_pressure = _extract_feature(feature_row, "support_pressure_score", 0.0)
        ema_bias = _extract_feature(feature_row, "ema_regime_bias", 0.0)
        fast_slow_gap = _extract_feature(feature_row, "fast_slow_gap_pct", 0.0)
        slow_trend_gap = _extract_feature(feature_row, "slow_trend_gap_pct", 0.0)
        rsi_value = _extract_feature(feature_row, "rsi", 50.0)

        min_trend_score = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_MIN_TREND_SCORE", 0.40) or 0.40
        )
        min_adx = float(getattr(config.ProductionConfig, "AI_MARKET_READING_MIN_ADX", 24.0) or 24.0)
        max_range_score = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_MAX_RANGE_SCORE", 0.74) or 0.74
        )
        near_level_pct = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_NEAR_LEVEL_PCT", 0.28) or 0.28
        )
        long_max_channel_position = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_LONG_MAX_CHANNEL_POSITION", 0.88) or 0.88
        )
        short_min_channel_position = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_SHORT_MIN_CHANNEL_POSITION", 0.12) or 0.12
        )
        pressure_threshold = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_PRESSURE_THRESHOLD", 0.82) or 0.82
        )
        learning_guard_min_trades = int(
            getattr(config.ProductionConfig, "AI_MARKET_READING_LEARNING_GUARD_MIN_TRADES", 10) or 10
        )
        learning_guard_min_win_rate_pct = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_LEARNING_GUARD_MIN_WIN_RATE_PCT", 48.0) or 48.0
        )
        learning_guard_max_avg_net_pct = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_LEARNING_GUARD_MAX_AVG_NET_PCT", -0.05) or -0.05
        )
        learning_guard_confidence_bonus = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_LEARNING_GUARD_CONFIDENCE_BONUS", 0.04) or 0.04
        )
        learning_guard_approval_bonus = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_LEARNING_GUARD_APPROVAL_BONUS", 0.05) or 0.05
        )
        learning_guard_margin_bonus = float(
            getattr(config.ProductionConfig, "AI_MARKET_READING_LEARNING_GUARD_MARGIN_BONUS", 0.05) or 0.05
        )
        learning_side_degraded = (
            learning_bias.trade_count >= learning_guard_min_trades
            and (
                learning_bias.win_rate_pct < learning_guard_min_win_rate_pct
                or learning_bias.avg_net_pct <= learning_guard_max_avg_net_pct
            )
        )
        if learning_side_degraded:
            min_confidence += learning_guard_confidence_bonus
            approval_threshold += learning_guard_approval_bonus
            min_action_margin += learning_guard_margin_bonus
        bullish_alignment = (
            regime_name in {"trend_bull", "weak_bull"}
            or (ema_bias > 0.0 and fast_slow_gap > 0.0 and slow_trend_gap > 0.0)
        )
        bearish_alignment = (
            regime_name in {"trend_bear", "weak_bear"}
            or (ema_bias < 0.0 and fast_slow_gap < 0.0 and slow_trend_gap < 0.0)
        )
        long_reversal_ready = (
            not bearish_alignment
            and ema_bias >= 0.0
            and range_score >= max_range_score
            and distance_to_low <= (near_level_pct * 1.15)
            and support_pressure >= (pressure_threshold - 0.08)
            and channel_position <= 0.28
            and rsi_value <= 38.0
            and ai_support >= (hold_prob + max(hold_edge, 0.08))
        )
        short_reversal_ready = (
            not bullish_alignment
            and ema_bias <= 0.0
            and range_score >= max_range_score
            and distance_to_high <= (near_level_pct * 1.15)
            and resistance_pressure >= (pressure_threshold - 0.08)
            and channel_position >= 0.72
            and rsi_value >= 62.0
            and ai_support >= (hold_prob + max(hold_edge, 0.08))
        )

        if combined_score < approval_threshold:
            return {
                "signal": "hold",
                "reason": (
                    f"market_reading_score_below_floor | score={combined_score:.2f}"
                    + (" | learning_guard=on" if learning_side_degraded else "")
                ),
                "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                "score": round(combined_score, 6),
                "atr": _extract_feature(feature_row, "atr", 0.0),
                "decision_source": "market_reading_hold",
                "ai_decision": _serialize_ai_decision(ai_decision),
                "learning_stats": asdict(learning_bias),
            }

        if action_margin < min_action_margin:
            return {
                "signal": "hold",
                "reason": (
                    "market_reading_action_margin_weak | "
                    f"margin={action_margin:.2f} | support={ai_support:.2f} | hold={hold_prob:.2f}"
                    + (" | learning_guard=on" if learning_side_degraded else "")
                ),
                "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                "score": round(combined_score, 6),
                "atr": _extract_feature(feature_row, "atr", 0.0),
                "decision_source": "market_reading_hold",
                "ai_decision": _serialize_ai_decision(ai_decision),
                "learning_stats": asdict(learning_bias),
            }

        if side == "long":
            context_supported = (
                bullish_alignment
                or long_reversal_ready
                or (
                    trend_score >= min_trend_score
                    and adx_value >= min_adx
                    and ema_bias >= 0.0
                )
            )
            range_blocked = range_score > max_range_score and trend_score < min_trend_score and adx_value < min_adx
            resistance_blocked = distance_to_high <= near_level_pct and (
                channel_position >= long_max_channel_position or resistance_pressure >= pressure_threshold
            )
            countertrend_blocked = bearish_alignment and not long_reversal_ready
            if learning_side_degraded and not bullish_alignment:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_long_learning_guard_blocked | "
                        f"wr={learning_bias.win_rate_pct:.2f} | avg={learning_bias.avg_net_pct:.3f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if countertrend_blocked:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_long_countertrend_blocked | "
                        f"regime={regime_name} | ema_bias={ema_bias:.2f} | rsi={rsi_value:.2f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if not context_supported:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_long_context_weak | "
                        f"regime={regime_name} | adx={adx_value:.2f} | trend={trend_score:.2f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if range_blocked:
                return {
                    "signal": "hold",
                    "reason": f"market_reading_long_range_crowded | range={range_score:.2f} | trend={trend_score:.2f}",
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if resistance_blocked:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_long_near_resistance | "
                        f"dist={distance_to_high:.2f} | channel={channel_position:.2f} | pressure={resistance_pressure:.2f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
        else:
            context_supported = (
                bearish_alignment
                or short_reversal_ready
                or (
                    trend_score >= min_trend_score
                    and adx_value >= min_adx
                    and ema_bias <= 0.0
                )
            )
            range_blocked = range_score > max_range_score and trend_score < min_trend_score and adx_value < min_adx
            support_blocked = distance_to_low <= near_level_pct and (
                channel_position <= short_min_channel_position or support_pressure >= pressure_threshold
            )
            countertrend_blocked = bullish_alignment and not short_reversal_ready
            if learning_side_degraded and not bearish_alignment:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_short_learning_guard_blocked | "
                        f"wr={learning_bias.win_rate_pct:.2f} | avg={learning_bias.avg_net_pct:.3f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if countertrend_blocked:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_short_countertrend_blocked | "
                        f"regime={regime_name} | ema_bias={ema_bias:.2f} | rsi={rsi_value:.2f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if not context_supported:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_short_context_weak | "
                        f"regime={regime_name} | adx={adx_value:.2f} | trend={trend_score:.2f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if range_blocked:
                return {
                    "signal": "hold",
                    "reason": f"market_reading_short_range_crowded | range={range_score:.2f} | trend={trend_score:.2f}",
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }
            if support_blocked:
                return {
                    "signal": "hold",
                    "reason": (
                        "market_reading_short_near_support | "
                        f"dist={distance_to_low:.2f} | channel={channel_position:.2f} | pressure={support_pressure:.2f}"
                    ),
                    "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
                    "score": round(combined_score, 6),
                    "atr": _extract_feature(feature_row, "atr", 0.0),
                    "decision_source": "market_reading_hold",
                    "ai_decision": _serialize_ai_decision(ai_decision),
                    "learning_stats": asdict(learning_bias),
                }

        return {
            "signal": ai_signal,
            "reason": (
                f"{setup_name}_pass | regime={regime_name} | ai={confidence:.2f} | "
                f"score={combined_score:.2f} | margin={action_margin:.2f} | "
                f"trend={trend_score:.2f} | range={range_score:.2f}"
            ),
            "setup": {"setup": setup_name, "direction": side, "regime": regime, "source": "ai_market_reading"},
            "score": round(combined_score * 10.0, 4),
            "atr": _extract_feature(feature_row, "atr", 0.0),
            "decision_source": "market_reading_ai",
            "ai_decision": _serialize_ai_decision(ai_decision),
            "hybrid_score": round(combined_score, 6),
            "learning_stats": asdict(learning_bias),
            "market_context": market_context or {},
        }

    def decide_entry(self, candle_slice, params: StrategyParams, *, feature_row=None) -> dict:
        working_slice = candle_slice
        required_columns = {"ema_fast", "ema_slow", "ema_trend", "rsi", "adx", "atr", "atr_pct", "vol_ma"}
        if not required_columns.issubset(set(getattr(candle_slice, "columns", []))):
            working_slice = calculate_indicators(candle_slice.copy(), params)
        resolved_feature_row = feature_row
        if resolved_feature_row is None:
            try:
                feature_frame = prepare_feature_frame(working_slice.copy(), params)
                if not feature_frame.empty:
                    resolved_feature_row = feature_frame.iloc[-1]
            except Exception:
                resolved_feature_row = None

        ai_enabled = bool(getattr(config.ProductionConfig, "ENABLE_AI_ASSISTANT", True))
        ai_mode = _resolve_assist_mode()
        if not ai_enabled and ai_mode != "market_reading":
            engine_result = generate_entry_signal(working_slice, params)
            resolved = dict(engine_result)
            resolved["decision_source"] = "engine_only"
            return resolved

        market_context = None
        if self.use_live_context and bool(getattr(config.ProductionConfig, "AI_WEB_CONTEXT_ENABLED", True)):
            try:
                market_context = self.market_context_service.get_context(self.symbol)
            except Exception as exc:
                market_context = {
                    "symbol": self.symbol,
                    "fear_greed": {"available": False, "reason": str(exc)},
                    "news": {"available": False, "reason": str(exc)},
                    "bias": {"long_bias": 0.0, "short_bias": 0.0, "caution_bias": 0.0, "reasons": []},
                }

        if feature_row is None:
            ai_decision = self.ai_model.score_candle_slice(
                working_slice,
                symbol=self.symbol,
                timeframe=self.timeframe,
                market_context=market_context,
            )
        else:
            ai_decision = self.ai_model.score_feature_row(
                feature_row,
                symbol=self.symbol,
                timeframe=self.timeframe,
                market_context=market_context,
            )
        if ai_mode == "market_reading":
            return self._build_market_reading_result(
                working_slice=working_slice,
                params=params,
                feature_row=resolved_feature_row,
                ai_decision=ai_decision,
                market_context=market_context,
            )

        engine_result = generate_entry_signal(working_slice, params)
        setup_payload = engine_result.get("setup") or {}
        setup_name = str(setup_payload.get("setup") or "unknown")
        signal = str(engine_result.get("signal") or "hold").strip().lower()
        resolved = dict(engine_result)
        resolved["ai_decision"] = _serialize_ai_decision(ai_decision)

        if signal not in {"buy", "sell"}:
            resolved["decision_source"] = "engine_hold"
            return resolved

        side = _signal_to_side(signal)
        ai_probs = ai_decision.get("probabilities") or {}
        ai_support = _safe_float(ai_probs.get("long" if side == "long" else "short"), 0.0)
        ai_opposition = _safe_float(ai_probs.get("short" if side == "long" else "long"), 0.0)
        ai_signal = str(ai_decision.get("signal") or "").strip().lower()
        if ai_signal == signal:
            alignment_bonus = 0.02
        elif ai_signal == "hold":
            alignment_bonus = 0.0
        else:
            alignment_bonus = -0.05
        learning_bias = self.learning_service.get_bias(
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=side,
            setup_name=setup_name,
        )
        combined_score = ai_support + alignment_bonus + learning_bias.bias - (ai_opposition * 0.05)
        approval_threshold = float(getattr(config.ProductionConfig, "AI_HYBRID_APPROVAL_THRESHOLD", 0.34) or 0.34)

        resolved["decision_source"] = "hybrid_ai"
        resolved["hybrid_score"] = round(combined_score, 6)
        resolved["hybrid_support"] = round(ai_support, 6)
        resolved["hybrid_opposition"] = round(ai_opposition, 6)
        resolved["learning_bias"] = round(learning_bias.bias, 6)
        resolved["learning_stats"] = asdict(learning_bias)
        setup_guard = self.learning_service.get_setup_guard(
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=side,
            setup_name=setup_name,
        )
        resolved["setup_guard"] = asdict(setup_guard)

        if setup_guard.blocked:
            self.learning_service.consume_setup_signal(
                symbol=self.symbol,
                timeframe=self.timeframe,
                side=side,
                setup_name=setup_name,
            )
            return {
                "signal": "hold",
                "reason": (
                    f"hybrid_blocked_setup_guard | base={signal} | setup={setup_name} | "
                    f"guard={setup_guard.reason} | cooldown={setup_guard.cooldown_remaining} | "
                    f"recent_pf={setup_guard.recent_profit_factor:.2f} | recent_avg={setup_guard.recent_avg_net_pct:.2f}"
                ),
                "setup": engine_result.get("setup"),
                "score": engine_result.get("score"),
                "atr": engine_result.get("atr"),
                "decision_source": "hybrid_blocked",
                "ai_decision": _serialize_ai_decision(ai_decision),
                "baseline_signal": signal,
                "baseline_reason": engine_result.get("reason"),
                "hybrid_score": round(combined_score, 6),
                "learning_stats": asdict(learning_bias),
                "setup_guard": asdict(setup_guard),
            }

        if bool(getattr(config.ProductionConfig, "AI_ENTRY_STRUCTURE_GUARD_ENABLED", True)):
            if signal == "buy" and setup_name == "pullback_long":
                adx_value = _extract_feature(resolved_feature_row, "adx", 0.0)
                trend_score = _extract_feature(resolved_feature_row, "trend_regime_score", 0.0)
                range_score = _extract_feature(resolved_feature_row, "range_regime_score", 0.0)
                min_adx = float(getattr(config.ProductionConfig, "AI_ENTRY_PULLBACK_LONG_MIN_ADX", 39.0) or 39.0)
                min_trend_score = float(
                    getattr(config.ProductionConfig, "AI_ENTRY_PULLBACK_LONG_MIN_TREND_SCORE", 0.42) or 0.42
                )
                max_range_score = float(
                    getattr(config.ProductionConfig, "AI_ENTRY_PULLBACK_LONG_MAX_RANGE_SCORE", 0.60) or 0.60
                )
                weak_context = trend_score < min_trend_score or range_score > max_range_score
                if adx_value < min_adx and weak_context:
                    return {
                        "signal": "hold",
                        "reason": (
                            "structure_block_pullback_long_weak_context | "
                            f"adx={adx_value:.2f} | trend={trend_score:.2f} | range={range_score:.2f}"
                        ),
                        "setup": engine_result.get("setup"),
                        "score": engine_result.get("score"),
                        "atr": engine_result.get("atr"),
                        "decision_source": "hybrid_blocked",
                        "ai_decision": _serialize_ai_decision(ai_decision),
                        "baseline_signal": signal,
                        "baseline_reason": engine_result.get("reason"),
                        "hybrid_score": round(combined_score, 6),
                        "learning_stats": asdict(learning_bias),
                    }
            if signal == "sell" and setup_name == "trend_resume_short":
                support_distance = _extract_feature(resolved_feature_row, "distance_to_rolling_low_pct", 99.0)
                channel_position = _extract_feature(resolved_feature_row, "channel_position_32", 1.0)
                range_score = _extract_feature(resolved_feature_row, "range_regime_score", 0.0)
                support_near_pct = float(
                    getattr(config.ProductionConfig, "AI_ENTRY_TREND_RESUME_SHORT_SUPPORT_NEAR_PCT", 0.45) or 0.45
                )
                max_channel_position = float(
                    getattr(config.ProductionConfig, "AI_ENTRY_TREND_RESUME_SHORT_MAX_CHANNEL_POSITION", 0.20) or 0.20
                )
                min_range_score = float(
                    getattr(config.ProductionConfig, "AI_ENTRY_TREND_RESUME_SHORT_MIN_RANGE_SCORE", 0.55) or 0.55
                )
                support_crowded = channel_position <= max_channel_position or range_score >= min_range_score
                if support_distance <= support_near_pct and support_crowded:
                    return {
                        "signal": "hold",
                        "reason": (
                            "structure_block_trend_resume_short_near_support | "
                            f"support={support_distance:.2f} | channel={channel_position:.2f} | range={range_score:.2f}"
                        ),
                        "setup": engine_result.get("setup"),
                        "score": engine_result.get("score"),
                        "atr": engine_result.get("atr"),
                        "decision_source": "hybrid_blocked",
                        "ai_decision": _serialize_ai_decision(ai_decision),
                        "baseline_signal": signal,
                        "baseline_reason": engine_result.get("reason"),
                        "hybrid_score": round(combined_score, 6),
                        "learning_stats": asdict(learning_bias),
                    }

        strong_opposition = (
            ai_signal in {"buy", "sell"}
            and ai_signal != signal
            and ai_opposition >= max(float(getattr(config.ProductionConfig, "AI_MIN_SIGNAL_CONFIDENCE", 0.40) or 0.40), ai_support + 0.08)
        )
        learning_degraded = learning_bias.trade_count >= self.learning_service.min_trades and learning_bias.bias <= -0.06

        if strong_opposition:
            return {
                "signal": "hold",
                "reason": (
                    f"hybrid_blocked_opposition | base={signal} | ai={ai_signal}:{ai_opposition:.2f} | "
                    f"support={ai_support:.2f}"
                ),
                "setup": engine_result.get("setup"),
                "score": engine_result.get("score"),
                "atr": engine_result.get("atr"),
                "decision_source": "hybrid_blocked",
                "ai_decision": _serialize_ai_decision(ai_decision),
                "baseline_signal": signal,
                "baseline_reason": engine_result.get("reason"),
                "hybrid_score": round(combined_score, 6),
                "learning_stats": asdict(learning_bias),
            }

        if learning_degraded and ai_support < approval_threshold:
            return {
                "signal": "hold",
                "reason": (
                    f"hybrid_blocked_learning | base={signal} | support={ai_support:.2f} | "
                    f"learn={learning_bias.bias:.2f}"
                ),
                "setup": engine_result.get("setup"),
                "score": engine_result.get("score"),
                "atr": engine_result.get("atr"),
                "decision_source": "hybrid_blocked",
                "ai_decision": _serialize_ai_decision(ai_decision),
                "baseline_signal": signal,
                "baseline_reason": engine_result.get("reason"),
                "hybrid_score": round(combined_score, 6),
                "learning_stats": asdict(learning_bias),
            }

        resolved["reason"] = (
            f"{engine_result.get('reason')} | hybrid_pass={combined_score:.2f} | "
            f"ai_support={ai_support:.2f} | learn={learning_bias.bias:.2f}"
        )
        return resolved

    def should_exit_position(self, *, position: dict | None, candle_slice, feature_row=None) -> dict:
        if not position or not bool(getattr(config.ProductionConfig, "AI_ALLOW_EARLY_EXIT", True)):
            return {"exit": False}
        ai_enabled = bool(getattr(config.ProductionConfig, "ENABLE_AI_ASSISTANT", True))
        if not ai_enabled:
            return {"exit": False}

        working_slice = candle_slice
        required_columns = {"ema_fast", "ema_slow", "ema_trend", "rsi", "adx", "atr", "atr_pct", "vol_ma"}
        if not required_columns.issubset(set(getattr(candle_slice, "columns", []))):
            working_slice = calculate_indicators(candle_slice.copy(), StrategyParams())

        market_context = None
        if self.use_live_context and bool(getattr(config.ProductionConfig, "AI_WEB_CONTEXT_ENABLED", True)):
            try:
                market_context = self.market_context_service.get_context(self.symbol)
            except Exception:
                market_context = None
        resolved_feature_row = feature_row
        if resolved_feature_row is None:
            try:
                feature_frame = prepare_feature_frame(working_slice.copy(), StrategyParams())
                if not feature_frame.empty:
                    resolved_feature_row = feature_frame.iloc[-1]
            except Exception:
                resolved_feature_row = None
        if feature_row is None:
            ai_decision = self.ai_model.score_candle_slice(
                working_slice,
                symbol=self.symbol,
                timeframe=self.timeframe,
                market_context=market_context,
            )
        else:
            ai_decision = self.ai_model.score_feature_row(
                feature_row,
                symbol=self.symbol,
                timeframe=self.timeframe,
                market_context=market_context,
            )
        side = str(position.get("side") or "").strip().lower()
        setup_name = str(
            position.get("entry_setup")
            or position.get("management_profile")
            or position.get("entry_source_setup")
            or "unknown"
        )
        learning_bias = self.learning_service.get_bias(
            symbol=self.symbol,
            timeframe=self.timeframe,
            side=side,
            setup_name=setup_name,
        )
        exit_signal = self.ai_model.should_exit_position(
            position=position,
            ai_decision=ai_decision,
            min_confidence=float(getattr(config.ProductionConfig, "AI_EXIT_MIN_SIGNAL_CONFIDENCE", 0.45) or 0.45),
            feature_row=resolved_feature_row,
            market_context=market_context,
            learning_bias=learning_bias.bias,
        )
        exit_signal["learning_stats"] = asdict(learning_bias)
        if exit_signal.get("exit"):
            exit_signal["ai_decision"] = _serialize_ai_decision(ai_decision)
            return exit_signal
        return {
            "exit": False,
            "ai_decision": _serialize_ai_decision(ai_decision),
            "monitor_reason": exit_signal.get("monitor_reason"),
            "structure": exit_signal.get("structure"),
            "learning_stats": asdict(learning_bias),
        }

    def register_trade_outcome(self, trade: dict) -> None:
        self.learning_service.register_trade_payload(
            symbol=self.symbol,
            timeframe=self.timeframe,
            trade=trade,
        )
