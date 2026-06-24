from __future__ import annotations

import json
import logging
import math
import os
import sys
import time
from datetime import UTC, datetime

import config
from database.database import db
from market_data import fetch_historical_candles_from_csv
from position_manager import (
    create_position,
    create_native_bracket_position,
    evaluate_open_position,
    evaluate_managed_position_on_candle,
    evaluate_native_bracket_position_on_candle,
)
from services.live_execution_service import LiveExecutionService
from services.unified_decision_engine import UnifiedDecisionEngine
from services.risk_management_service import RiskManagementService
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal, get_min_required_rows
from trading_bot_websocket import StreamlinedTradingBot
from runtime_process import (
    clear_runtime_process_state,
    clear_runtime_stop_request,
    get_runtime_execution_log_path,
    get_runtime_process_state_path,
    get_runtime_stop_request_path,
    runtime_stop_requested,
    write_runtime_process_state,
)

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())
logger.propagate = False
_RUNTIME_LOGGING_CONFIGURED = False


def _configure_runtime_logging() -> None:
    global _RUNTIME_LOGGING_CONFIGURED
    if _RUNTIME_LOGGING_CONFIGURED:
        return

    os.makedirs("logs", exist_ok=True)
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    runtime_log_path = str(os.getenv("BOT_EXECUTION_LOG_PATH") or get_runtime_execution_log_path(_runtime_key()))
    os.makedirs(str(os.path.dirname(runtime_log_path) or "logs"), exist_ok=True)
    file_handler = logging.FileHandler(runtime_log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)

    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    _RUNTIME_LOGGING_CONFIGURED = True

def log_info(*parts):
    if not parts:
        return
    logger.info(" ".join(str(part) for part in parts))


def _sleep_with_stop(seconds: float) -> None:
    deadline = time.time() + max(float(seconds or 0.0), 0.0)
    while time.time() < deadline:
        if runtime_stop_requested(path=_runtime_stop_request_path()):
            return
        time.sleep(min(1.0, max(deadline - time.time(), 0.0)))


def _resolve_day(value) -> str:
    if value is None:
        return datetime.now(UTC).date().isoformat()
    day_fn = getattr(value, "date", None)
    if callable(day_fn):
        try:
            return day_fn().isoformat()
        except Exception:
            pass
    try:
        return datetime.fromisoformat(str(value)).date().isoformat()
    except Exception:
        return datetime.now(UTC).date().isoformat()


def _validate_real_mode_guards() -> None:
    mode_label = "TESTNET" if bool(config.TESTNET) else "CONTA REAL"
    log_info(f"Inicializando bot | modo: {mode_label} | symbol: {config.SYMBOL} | timeframe: {config.TIMEFRAME}")

    if bool(config.TESTNET):
        return

    if not bool(config.ProductionConfig.ENABLE_LIVE_EXECUTION):
        raise RuntimeError("Modo real bloqueado: ENABLE_LIVE_EXECUTION=false.")

    confirmation = str(getattr(config, "LIVE_TRADING_CONFIRMATION", "") or "").strip().upper()
    if confirmation != "EU_ASSUMO_RISCO":
        raise RuntimeError("Modo real exige LIVE_TRADING_CONFIRMATION=EU_ASSUMO_RISCO.")

    risk_per_trade = float(getattr(config, "RISK_PER_TRADE_PCT", 0.0) or 0.0)
    max_risk_start = float(getattr(config, "MAX_REAL_RISK_PER_TRADE_PCT_START", 0.25) or 0.25)
    if risk_per_trade > max_risk_start:
        raise RuntimeError(
            f"Risco por trade acima do limite de go-live ({risk_per_trade:.2f}% > {max_risk_start:.2f}%)."
        )

    log_info(
        "Guard-rails real OK | "
        f"risk_per_trade={risk_per_trade:.2f}% | "
        f"max_daily_loss={float(getattr(config, 'MAX_DAILY_REAL_LOSS_PCT', 2.5)):.2f}% | "
        f"max_consecutive_losses={int(getattr(config, 'MAX_CONSECUTIVE_REAL_LOSSES', 4))}"
    )


def _validate_runtime_symbol_approval() -> None:
    if not bool(getattr(config, "RUNTIME_REQUIRE_APPROVED_SYMBOL", True)):
        return
    record = config.get_symbol_validation_record(config.SYMBOL)
    if config.is_symbol_runtime_approved(config.SYMBOL):
        label = str(record.get("approval_label") or "approved").strip() or "approved"
        log_info(f"Governanca de simbolo OK | {config.SYMBOL} | status={label}")
        return
    status = str(record.get("status") or "unknown").strip().lower()
    if bool(config.TESTNET) and bool(getattr(config, "RUNTIME_ALLOW_WATCHLIST_IN_TESTNET", True)) and status == "watchlist":
        log_info(f"Governanca de simbolo em observacao | {config.SYMBOL} | status=watchlist | permitido em TESTNET")
        return
    if not record:
        raise RuntimeError(
            f"Simbolo {config.SYMBOL} sem validacao aprovada. Rode validate_symbol_universe.py antes do runtime."
        )
    status = str(record.get("status") or "unknown").strip() or "unknown"
    reason = str(record.get("reason") or record.get("summary") or "sem detalhe").strip() or "sem detalhe"
    raise RuntimeError(
        f"Simbolo {config.SYMBOL} bloqueado pela governanca ({status}). Motivo: {reason}"
    )


def _print_runtime_baseline_snapshot() -> dict:
    snapshot = config.build_runtime_strategy_snapshot()
    headline = (
        "Baseline ativo | "
        f"version={snapshot['strategy_version']} | "
        f"rsi={snapshot['rsi_period']}:{int(snapshot['buy_rsi_signal'])}/{int(snapshot['sell_rsi_signal'])} | "
        f"long={'on' if snapshot['allow_long'] else 'off'} | "
        f"short={'on' if snapshot['allow_short'] else 'off'} | "
        f"unknown_block={'on' if snapshot['block_unknown_regime'] else 'off'}"
    )
    log_info(headline)
    log_info(f"Runtime snapshot: {json.dumps(snapshot, ensure_ascii=True, sort_keys=True)}")
    return snapshot


def _runtime_key() -> str:
    explicit_key = str(os.getenv("TRADER_BOT_RUNTIME_KEY") or "").strip()
    if explicit_key:
        return explicit_key
    user_id = int(getattr(config, "SINGLE_USER_RUNTIME_USER_ID", 0) or 0)
    account_id = str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "") or "").strip()
    if user_id or account_id:
        return f"account:{user_id}:{account_id or 'default'}:{config.SYMBOL}:{config.TIMEFRAME}"
    return f"primary:{config.SYMBOL}:{config.TIMEFRAME}"


def _runtime_process_state_path():
    return get_runtime_process_state_path(_runtime_key())


def _runtime_stop_request_path():
    return get_runtime_stop_request_path(_runtime_key())


def _live_execution_enabled() -> bool:
    return bool(config.ProductionConfig.ENABLE_LIVE_EXECUTION)


def _paper_tracking_enabled() -> bool:
    return not _live_execution_enabled()


def _build_single_user_execution_context() -> dict:
    exchange_name = str(getattr(config, "SINGLE_USER_RUNTIME_EXCHANGE", "") or "binanceusdm").strip() or "binanceusdm"
    account_id = str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "") or "env-primary").strip() or "env-primary"
    account_alias = (
        str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ALIAS", "") or "Primary Env Account").strip() or account_id
    )
    user_id = int(getattr(config, "SINGLE_USER_RUNTIME_USER_ID", 0) or 0)
    return {
        "user_id": user_id,
        "account_id": account_id,
        "account_alias": account_alias,
        "exchange_name": exchange_name,
        "exchange": exchange_name,
        "live_enabled": True,
        "paper_enabled": bool(config.TESTNET),
        "use_env_credentials": True,
        "credential_source": "env",
    }


def _signal_to_position_side(signal: str) -> str:
    return "long" if str(signal).strip().lower() in {"buy", "long", "compra"} else "short"


def _opposite_signal_for_position(position: dict) -> str:
    return "sell" if str(position.get("side") or "").strip().lower() == "long" else "buy"


def _resolve_position_execution_profile(position: dict | None) -> str:
    if not position:
        return "managed"
    explicit_profile = str(position.get("execution_profile") or "").strip().lower()
    if explicit_profile:
        return explicit_profile
    if str(position.get("execution_mode") or "").strip().lower() == "live":
        return "native_bracket"
    return "managed"


def _resolve_runtime_execution_profile(explicit_profile: str | None = None) -> str:
    if explicit_profile is not None and str(explicit_profile).strip():
        candidate = str(explicit_profile).strip().lower()
    else:
        candidate = str(getattr(config, "EXECUTION_PROFILE", "") or "").strip().lower()
    if candidate == "native_bracket":
        return "native_bracket"
    return "managed"


def _resolve_ai_assist_mode() -> str:
    if not bool(getattr(config.ProductionConfig, "ENABLE_AI_ASSISTANT", False)):
        return "disabled"
    candidate = str(getattr(config.ProductionConfig, "AI_ASSIST_MODE", "disabled") or "disabled").strip().lower()
    if candidate in {"shadow", "filter", "full"}:
        return candidate
    return "disabled"


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


def _resolve_runtime_signal_decision(
    *,
    engine_result: dict,
    ai_decision: dict | None,
    ai_mode: str,
    min_confidence: float,
) -> dict:
    if ai_mode == "disabled" or not ai_decision or not ai_decision.get("enabled"):
        resolved = dict(engine_result)
        resolved["decision_source"] = "engine"
        return resolved

    resolved_engine = dict(engine_result)
    resolved_engine["decision_source"] = "engine"
    resolved_engine["ai_decision"] = _serialize_ai_decision(ai_decision)

    ai_signal = str(ai_decision.get("signal") or "hold").strip().lower()
    ai_confidence = float(ai_decision.get("confidence", 0.0) or 0.0)

    if ai_mode == "shadow":
        return resolved_engine

    if ai_mode == "filter":
        if resolved_engine.get("signal") not in {"buy", "sell"}:
            return resolved_engine
        if ai_signal == resolved_engine.get("signal") and ai_confidence >= min_confidence:
            resolved_engine["decision_source"] = "engine_ai_filter_pass"
            resolved_engine["reason"] = (
                f"{resolved_engine.get('reason')} | ai_ok={ai_signal}:{ai_confidence:.2f}"
            )
            return resolved_engine
        return {
            "signal": "hold",
            "reason": (
                f"ai_filter_blocked | engine={resolved_engine.get('signal')} | "
                f"ai={ai_signal}:{ai_confidence:.2f}"
            ),
            "setup": resolved_engine.get("setup"),
            "score": resolved_engine.get("score"),
            "atr": resolved_engine.get("atr"),
            "decision_source": "ai_filter_blocked",
            "ai_decision": _serialize_ai_decision(ai_decision),
            "baseline_signal": resolved_engine.get("signal"),
            "baseline_reason": resolved_engine.get("reason"),
        }

    if ai_mode == "full":
        if ai_signal in {"buy", "sell"} and ai_confidence >= min_confidence:
            return {
                "signal": ai_signal,
                "reason": f"{ai_decision.get('reason')} | conf={ai_confidence:.2f}",
                "setup": {
                    "setup": "ai_runtime_full",
                    "source_setup": "ai_runtime_full",
                    "direction": "long" if ai_signal == "buy" else "short",
                    "regime": {"regime": "ai_controlled"},
                },
                "score": round(ai_confidence * 10.0, 4),
                "atr": resolved_engine.get("atr"),
                "decision_source": "ai_full",
                "ai_decision": _serialize_ai_decision(ai_decision),
                "baseline_signal": resolved_engine.get("signal"),
                "baseline_reason": resolved_engine.get("reason"),
            }
        return {
            "signal": "hold",
            "reason": f"ai_full_hold | ai={ai_signal}:{ai_confidence:.2f}",
            "setup": resolved_engine.get("setup"),
            "score": round(ai_confidence * 10.0, 4),
            "atr": resolved_engine.get("atr"),
            "decision_source": "ai_full_hold",
            "ai_decision": _serialize_ai_decision(ai_decision),
            "baseline_signal": resolved_engine.get("signal"),
            "baseline_reason": resolved_engine.get("reason"),
        }

    return resolved_engine


def _resolve_signal_setup_names(
    signal_result: dict | None = None,
    *,
    entry_setup: str | None = None,
    entry_source_setup: str | None = None,
) -> tuple[str, str]:
    setup_name = str(entry_setup or "")
    source_setup_name = str(entry_source_setup or "")
    setup_payload = (signal_result or {}).get("setup") or {}
    if not setup_name and isinstance(setup_payload, dict):
        setup_name = str(setup_payload.get("setup") or "")
    if not source_setup_name and isinstance(setup_payload, dict):
        source_setup_name = str(setup_payload.get("source_setup") or "")
    return setup_name, source_setup_name


def _attach_runtime_entry_context(position: dict, signal_result: dict | None = None) -> dict:
    if not position:
        return position
    enriched = dict(position)
    if not signal_result:
        return enriched
    setup_payload = signal_result.get("setup") or {}
    regime_payload = setup_payload.get("regime") if isinstance(setup_payload, dict) else {}
    enriched["entry_signal_reason"] = str(signal_result.get("reason") or "")
    enriched["entry_setup"] = setup_payload.get("setup") if isinstance(setup_payload, dict) else None
    enriched["entry_source_setup"] = setup_payload.get("source_setup") if isinstance(setup_payload, dict) else None
    enriched["entry_regime"] = regime_payload.get("regime") if isinstance(regime_payload, dict) else None
    return enriched


def _build_runtime_position(
    *,
    signal: str,
    entry_price: float,
    timestamp,
    atr: float,
    execution_profile: str | None = None,
    signal_result: dict | None = None,
    entry_setup: str | None = None,
    entry_source_setup: str | None = None,
) -> dict:
    resolved_profile = _resolve_runtime_execution_profile(execution_profile)
    setup_name, source_setup_name = _resolve_signal_setup_names(
        signal_result,
        entry_setup=entry_setup,
        entry_source_setup=entry_source_setup,
    )
    if resolved_profile == "native_bracket":
        position = create_native_bracket_position(
            signal=signal,
            entry_price=entry_price,
            timestamp=timestamp,
            atr=atr,
        )
        return _attach_runtime_entry_context(position, signal_result)
    position = create_position(
        signal=signal,
        entry_price=entry_price,
        timestamp=timestamp,
        atr=atr,
        entry_setup=setup_name,
        entry_source_setup=source_setup_name,
    )
    return _attach_runtime_entry_context(position, signal_result)


def _derive_runtime_trailing_active(position: dict | None) -> bool:
    if not position:
        return False
    try:
        current_stop = float(position.get("current_stop"))
        initial_stop = float(position.get("initial_stop"))
    except (TypeError, ValueError):
        return False
    return abs(current_stop - initial_stop) > 1e-12


def _resolve_runtime_position_stop_pct(position: dict | None) -> float:
    if not position:
        return 0.0
    entry_price = float(position.get("entry_price") or 0.0)
    stop_price = float(position.get("current_stop") or 0.0)
    if entry_price <= 0 or stop_price <= 0:
        return 0.0
    return abs(entry_price - stop_price) / entry_price * 100


def _finalize_runtime_managed_trade(position_before_close: dict, closed_trade: dict, realized_partial_pct: float) -> dict:
    trade = dict(closed_trade)
    cumulative_realized_partial = float(realized_partial_pct or 0.0)
    if cumulative_realized_partial > 0:
        trade["gross_pct"] = (float(trade.get("gross_pct", 0.0) or 0.0) * 0.5) + cumulative_realized_partial
    trade["realized_partial_pct"] = cumulative_realized_partial
    return trade


def _compact_runtime_symbol(symbol: str) -> str:
    return str(symbol or "").replace("/", "").replace(":", "").replace("-", "").upper()


def _calculate_trade_pct_from_position(position: dict, exit_price: float) -> float:
    entry_price = float(position.get("entry_price") or 0.0)
    if entry_price <= 0:
        return 0.0
    if str(position.get("side") or "").strip().lower() == "long":
        return ((float(exit_price) - entry_price) / entry_price) * 100
    return ((entry_price - float(exit_price)) / entry_price) * 100


def _position_market_alignment(position: dict | None, candle_slice) -> dict:
    if not position or candle_slice is None or getattr(candle_slice, "empty", True):
        return {"aligned": False, "reason": "missing_position_or_market_data"}
    try:
        row = candle_slice.iloc[-1]
    except Exception:
        return {"aligned": False, "reason": "missing_latest_candle"}

    side = str(position.get("side") or "").strip().lower()
    if side not in {"long", "short"}:
        return {"aligned": False, "reason": "invalid_position_side"}

    def _row_float(name: str, fallback: float = 0.0) -> float:
        try:
            return float(row.get(name, fallback) or fallback)
        except Exception:
            return float(fallback)

    close_price = _row_float("close")
    entry_price = float(position.get("entry_price") or 0.0)
    ema_fast = _row_float("ema_fast", close_price)
    ema_slow = _row_float("ema_slow", ema_fast)
    ema_trend = _row_float("ema_trend", ema_slow)
    rsi = _row_float("rsi", 50.0)
    trend_strength_pct = _row_float("trend_strength_pct", 0.0)
    if close_price <= 0 or entry_price <= 0:
        return {"aligned": False, "reason": "invalid_price"}

    unrealized_pct = _calculate_trade_pct_from_position(position, close_price)
    if side == "long":
        aligned = (
            close_price >= ema_fast
            and ema_fast >= ema_slow
            and close_price >= ema_trend
            and rsi >= 50.0
            and unrealized_pct >= 0.0
        )
    else:
        aligned = (
            close_price <= ema_fast
            and ema_fast <= ema_slow
            and close_price <= ema_trend
            and rsi <= 50.0
            and unrealized_pct >= 0.0
        )

    return {
        "aligned": bool(aligned),
        "reason": "market_still_favors_position" if aligned else "market_no_longer_favors_position",
        "side": side,
        "close": close_price,
        "entry_price": entry_price,
        "unrealized_pct": unrealized_pct,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "ema_trend": ema_trend,
        "rsi": rsi,
        "trend_strength_pct": trend_strength_pct,
    }


def _should_manage_aligned_position_with_trailing(position: dict | None, candle_slice, exit_signal: dict | None) -> tuple[bool, dict]:
    if not bool(getattr(config, "BOT_TRAILING_ONLY_WHEN_POSITION_ALIGNED", True)):
        return False, {}
    if not (exit_signal or {}).get("exit"):
        return False, {}
    alignment = _position_market_alignment(position, candle_slice)
    if not alignment.get("aligned"):
        return False, alignment
    return True, alignment


def _resolve_recent_exchange_fill(context: dict | None, *, expected_side: str | None = None) -> dict | None:
    if not context:
        return None
    try:
        events = db.get_user_execution_events(
            user_id=int(context["user_id"]),
            account_id=str(context["account_id"]),
            limit=50,
        )
    except Exception:
        return None

    target_symbol = _compact_runtime_symbol(config.SYMBOL)
    normalized_expected_side = str(expected_side or "").strip().lower()
    for event in events:
        if str(event.get("event_type") or "").strip().upper() != "ORDER_TRADE_UPDATE":
            continue
        payload = (event.get("details_json") or {}).get("payload") or {}
        order_payload = payload.get("o") if isinstance(payload, dict) else {}
        if not isinstance(order_payload, dict):
            continue
        event_symbol = _compact_runtime_symbol(order_payload.get("s") or payload.get("s") or "")
        if event_symbol != target_symbol:
            continue
        order_status = str(order_payload.get("X") or "").strip().upper()
        execution_type = str(order_payload.get("x") or "").strip().upper()
        if order_status != "FILLED" and execution_type not in {"TRADE", "FILLED"}:
            continue
        side = str(order_payload.get("S") or "").strip().lower()
        if normalized_expected_side and side != normalized_expected_side:
            continue
        fill_price = (
            order_payload.get("ap")
            or order_payload.get("L")
            or order_payload.get("p")
            or order_payload.get("sp")
        )
        try:
            resolved_price = float(fill_price or 0.0)
        except (TypeError, ValueError):
            resolved_price = 0.0
        if resolved_price <= 0:
            continue
        return {
            "price": resolved_price,
            "side": side,
            "order_type": str(order_payload.get("o") or order_payload.get("ot") or "").strip().lower(),
            "event_id": event.get("id"),
        }
    return None


def _build_exchange_reconciled_close(
    position: dict,
    candle_row,
    context: dict | None = None,
    reason: str = "exchange_reconciled",
) -> dict:
    entry_price = float(position.get("entry_price") or 0.0)
    reconciled_fill = _resolve_recent_exchange_fill(
        context,
        expected_side=_opposite_signal_for_position(position),
    )
    exit_price = float((reconciled_fill or {}).get("price") or candle_row["close"])
    gross_pct = _calculate_trade_pct_from_position(position, exit_price)
    resolved_reason = str((reconciled_fill or {}).get("order_type") or reason or "exchange_reconciled")
    return {
        "side": position.get("side"),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_timestamp": position.get("entry_timestamp"),
        "exit_timestamp": candle_row["timestamp"],
        "best_price": float(position.get("best_price") or entry_price),
        "gross_pct": gross_pct,
        "mfe_pct": float(position.get("mfe_pct", 0.0) or 0.0),
        "mae_pct": float(position.get("mae_pct", 0.0) or 0.0),
        "reason": resolved_reason,
    }


def _build_forced_runtime_close(position: dict, candle_row, *, reason: str) -> dict:
    entry_price = float(position.get("entry_price") or 0.0)
    exit_price = float(candle_row["close"])
    return {
        "side": position.get("side"),
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_timestamp": position.get("entry_timestamp"),
        "exit_timestamp": candle_row["timestamp"],
        "best_price": float(position.get("best_price") or entry_price),
        "gross_pct": _calculate_trade_pct_from_position(position, exit_price),
        "mfe_pct": float(position.get("mfe_pct", 0.0) or 0.0),
        "mae_pct": float(position.get("mae_pct", 0.0) or 0.0),
        "reason": str(reason or "ai_forced_exit"),
    }


def _register_learning_trade(unified_decision_engine: UnifiedDecisionEngine | None, trade: dict | None) -> None:
    if unified_decision_engine is None or not trade:
        return
    try:
        unified_decision_engine.register_trade_outcome(trade)
    except Exception as learning_error:
        log_info(f"Aviso: falha ao atualizar memoria adaptativa: {learning_error}")


def _resolve_stop_loss_pct(signal: str) -> float:
    return float(config.LONG_STOP_LOSS_PCT if _signal_to_position_side(signal) == "long" else config.SHORT_STOP_LOSS_PCT)

def _resolve_take_profit_pct(signal: str) -> float:
    return float(config.LONG_TAKE_PROFIT_PCT if _signal_to_position_side(signal) == "long" else config.SHORT_TAKE_PROFIT_PCT)


def _find_live_positions(context: dict) -> list[dict]:
    rows = db.get_user_live_positions(
        user_id=int(context["user_id"]),
        account_id=str(context["account_id"]),
        status="open",
    )
    exchange_name = str(context.get("exchange_name") or context.get("exchange") or "").strip().lower()
    symbol = str(config.SYMBOL).strip().upper()
    filtered = []
    for row in rows:
        row_symbol = str(row.get("symbol") or "").strip().upper()
        row_exchange = str(row.get("exchange") or "").strip().lower()
        if row_symbol != symbol:
            continue
        if exchange_name and row_exchange and row_exchange != exchange_name:
            continue
        filtered.append(row)
    return filtered


def _save_live_realization_event(
    *,
    context: dict,
    position: dict,
    trade: dict,
    gross_pct_contribution: float,
    event_type: str,
    event_status: str,
    message: str,
    source: str,
) -> None:
    try:
        planned_position_notional = float(
            position.get("planned_position_notional")
            or (float(position.get("entry_price") or 0.0) * float(position.get("quantity") or 0.0))
            or 0.0
        )
        account_reference_balance = float(position.get("account_reference_balance") or 0.0)
        realized_pnl = planned_position_notional * (float(gross_pct_contribution or 0.0) / 100.0)
        outcome = "WIN" if gross_pct_contribution > 0 else "LOSS" if gross_pct_contribution < 0 else "FLAT"
        db.save_user_execution_event(
            {
                "user_id": int(context["user_id"]),
                "account_id": str(context["account_id"]),
                "exchange": context.get("exchange_name") or context.get("exchange"),
                "symbol": config.SYMBOL,
                "timeframe": config.TIMEFRAME,
                "strategy_version": position.get("strategy_version"),
                "event_type": event_type,
                "event_status": event_status,
                "message": message,
                "details_json": {
                    "source": source,
                    "side": position.get("side"),
                    "entry_price": float(position.get("entry_price") or 0.0),
                    "exit_price": float(trade.get("exit_price") or 0.0),
                    "gross_pct": float(trade.get("gross_pct", 0.0) or 0.0),
                    "gross_pct_contribution": float(gross_pct_contribution or 0.0),
                    "planned_position_notional": planned_position_notional,
                    "account_reference_balance": account_reference_balance,
                    "realized_pnl": realized_pnl,
                    "execution_profile": position.get("execution_profile"),
                    "reason": trade.get("reason"),
                    "outcome": outcome,
                },
            }
        )
    except Exception as exc:
        log_info(f"Aviso: falha ao registrar realizacao live: {exc}")


def _refresh_live_managed_protective_stop(
    *,
    execution_service: LiveExecutionService,
    context: dict,
    position_before: dict,
    position_after: dict,
) -> dict:
    refreshed_position = dict(position_after)
    target_quantity = float(refreshed_position.get("quantity", 0.0) or 0.0)
    target_stop = float(refreshed_position.get("current_stop", 0.0) or 0.0)
    if target_quantity <= 0 or target_stop <= 0:
        return refreshed_position

    previous_stop_order_id = str(
        position_before.get("protective_stop_order_id")
        or refreshed_position.get("protective_stop_order_id")
        or ""
    ).strip()
    previous_stop_price = float(
        position_before.get("protective_stop_price")
        or position_before.get("current_stop")
        or 0.0
    )
    previous_quantity = float(position_before.get("quantity", 0.0) or 0.0)
    needs_refresh = (
        not previous_stop_order_id
        or abs(previous_stop_price - target_stop) > 1e-9
        or abs(previous_quantity - target_quantity) > 1e-12
    )
    if not needs_refresh:
        return refreshed_position

    replacement = execution_service.replace_stop_market_order(
        context=context,
        symbol=config.SYMBOL,
        side=_opposite_signal_for_position(refreshed_position),
        stop_price=target_stop,
        quantity=target_quantity,
        previous_order_id=previous_stop_order_id or None,
        testnet=bool(config.TESTNET),
        metadata={
            "reason": "managed_stop_refresh",
            "previous_stop_price": previous_stop_price,
            "new_stop_price": target_stop,
            "quantity": target_quantity,
        },
    )
    refreshed_position["protective_stop_order_id"] = replacement.get("exchange_order_id")
    refreshed_position["protective_stop_client_order_id"] = replacement.get("client_order_id")
    refreshed_position["protective_stop_price"] = float(replacement.get("stop_price") or target_stop)
    refreshed_position["last_stop_sync_at"] = datetime.now(UTC).isoformat()
    if replacement.get("previous_cancel_error"):
        log_info(
            "Aviso: stop anterior nao existia/nao foi localizado; limpeza preventiva executada antes do novo stop:",
            replacement.get("previous_cancel_error"),
        )
    else:
        log_info(
            f"Stop live sincronizado | SL: {float(refreshed_position['protective_stop_price']):.2f} | qty: {target_quantity:.6f}"
        )
    return refreshed_position


def _build_live_entry_plan(
    *,
    execution_service: LiveExecutionService,
    risk_management_service: RiskManagementService,
    context: dict,
    signal_side: str,
    entry_price: float,
    atr: float = 0.0,
    execution_profile: str | None = None,
    signal_result: dict | None = None,
    timestamp=None,
) -> dict:
    open_positions = _find_live_positions(context)
    max_open_real_trades = int(getattr(config, "MAX_OPEN_REAL_TRADES", 1) or 1)
    if len(open_positions) >= max_open_real_trades:
        return {
            "allowed": False,
            "reason": f"Limite de posicoes reais abertas atingido ({len(open_positions)}/{max_open_real_trades}).",
        }

    balance_snapshot = execution_service.fetch_account_balance_snapshot(
        context,
        quote_asset="USDT",
        testnet=bool(config.TESTNET),
    )
    trading_rules = execution_service.fetch_symbol_trading_rules(
        context,
        symbol=config.SYMBOL,
        testnet=bool(config.TESTNET),
    )
    account_balance = float(balance_snapshot.get("total") or 0.0)
    if account_balance <= 0:
        account_balance = float(balance_snapshot.get("free") or 0.0)
    if account_balance <= 0:
        return {
            "allowed": False,
            "reason": "Saldo indisponivel para calcular a ordem live.",
            "balance_snapshot": balance_snapshot,
        }

    preview_position = _build_runtime_position(
        signal=signal_side,
        entry_price=float(entry_price),
        timestamp=timestamp,
        atr=float(atr or 0.0),
        execution_profile=execution_profile,
        signal_result=signal_result,
    )
    stop_loss_price = float(preview_position.get("current_stop") or 0.0)
    take_profit_price = float(preview_position.get("partial_target") or 0.0)
    sl_pct = _resolve_runtime_position_stop_pct(preview_position)

    risk_data = risk_management_service.build_trade_plan(
        entry_price=float(entry_price),
        stop_loss_pct=float(sl_pct),
        symbol=config.SYMBOL,
        timeframe=config.TIMEFRAME,
        strategy_version=None,
        account_balance=account_balance,
        risk_per_trade_pct=float(config.RISK_PER_TRADE_PCT),
        max_open_trades=max_open_real_trades,
        execution_scope="live",
        live_context=context,
    )

    if not bool(risk_data.get("allowed", False)):
        return {
            "allowed": False,
            "reason": f"Risco negado: {risk_data.get('reason') or risk_data.get('risk_reason') or 'bloqueado'}",
            "balance_snapshot": balance_snapshot,
            "risk_data": risk_data,
        }

    quantity = float(risk_data.get("quantity", 0.0) or 0.0)
    if quantity <= 0:
        return {
            "allowed": False,
            "reason": "Quantidade calculada zerada para a ordem live.",
            "balance_snapshot": balance_snapshot,
            "risk_data": risk_data,
        }

    operability = risk_management_service.evaluate_symbol_operability(
        entry_price=float(entry_price),
        stop_loss_pct=float(sl_pct),
        risk_pct=float(risk_data.get("risk_per_trade_pct", 0.0) or 0.0),
        quantity=quantity,
        position_notional=float(risk_data.get("position_notional", 0.0) or 0.0),
        trading_rules=trading_rules,
        leverage=float(risk_data.get("leverage") or getattr(config, "LEVERAGE", 1) or 1),
        sizing_mode=str(risk_data.get("sizing_mode") or getattr(config, "POSITION_SIZING_MODE", "risk")),
        margin_allocation_pct=float(
            risk_data.get("margin_allocation_pct")
            or getattr(config, "POSITION_MARGIN_ALLOCATION_PCT", 0.0)
            or 0.0
        ),
    )
    if not bool(operability.get("allowed", False)):
        return {
            "allowed": False,
            "reason": f"Operabilidade negada: {operability.get('reason') or 'ordem abaixo do minimo da exchange'}",
            "balance_snapshot": balance_snapshot,
            "risk_data": risk_data,
            "trading_rules": trading_rules,
            "operability": operability,
        }
    resolved_quantity = float(operability.get("rounded_quantity") or quantity or 0.0)
    resolved_position_notional = float(
        operability.get("rounded_notional") or risk_data.get("position_notional", 0.0) or 0.0
    )

    return {
        "allowed": True,
        "reason": "",
        "account_balance": account_balance,
        "balance_snapshot": balance_snapshot,
        "trading_rules": trading_rules,
        "operability": operability,
        **risk_data,
        "quantity": resolved_quantity,
        "position_notional": resolved_position_notional,
        "execution_profile": _resolve_runtime_execution_profile(execution_profile),
        "preview_position": preview_position,
        "take_profit_price": take_profit_price,
        "stop_loss_price": stop_loss_price,
    }


def _quantity_after_exchange_step(quantity: float, trading_rules: dict | None) -> float:
    rules = trading_rules if isinstance(trading_rules, dict) else {}
    resolved_quantity = max(float(quantity or 0.0), 0.0)
    qty_step = max(float(rules.get("qty_step", 0.0) or 0.0), 0.0)
    qty_precision = rules.get("qty_precision")
    if qty_step > 0:
        return max(math.floor((resolved_quantity + 1e-12) / qty_step) * qty_step, 0.0)
    if qty_precision not in (None, ""):
        return max(round(resolved_quantity, int(qty_precision)), 0.0)
    return resolved_quantity


def _is_reduce_only_quantity_operable(
    *,
    quantity: float,
    reference_price: float,
    trading_rules: dict | None,
) -> tuple[bool, dict]:
    rules = trading_rules if isinstance(trading_rules, dict) else {}
    rounded_quantity = _quantity_after_exchange_step(quantity, rules)
    rounded_notional = rounded_quantity * max(float(reference_price or 0.0), 0.0)
    min_qty = max(float(rules.get("min_qty", 0.0) or 0.0), 0.0)
    min_notional = max(float(rules.get("min_notional", 0.0) or 0.0), 0.0)
    details = {
        "requested_quantity": round(float(quantity or 0.0), 12),
        "rounded_quantity": round(rounded_quantity, 12),
        "rounded_notional": round(rounded_notional, 8),
        "min_qty": round(min_qty, 12),
        "min_notional": round(min_notional, 8),
    }
    if rounded_quantity <= 0:
        return False, {**details, "reason": "quantidade_reduce_only_zerada_apos_arredondamento"}
    if min_qty > 0 and rounded_quantity < min_qty:
        return False, {**details, "reason": "quantidade_reduce_only_abaixo_min_qty"}
    if min_notional > 0 and rounded_notional < min_notional:
        return False, {**details, "reason": "notional_reduce_only_abaixo_minimo"}
    return True, details


def _prepare_live_execution_runtime(
    *,
    snapshot: dict,
    execution_service: LiveExecutionService,
    recovered_position: dict | None,
) -> tuple[dict, dict | None, object]:
    context = _build_single_user_execution_context()
    validation = execution_service.validate_account_connection(context, testnet=bool(config.TESTNET))
    if not validation.get("ok"):
        raise RuntimeError(f"Falha ao validar credenciais live do runner: {validation.get('error') or 'erro desconhecido'}")

    reconciliation = execution_service.reconcile_account_state(
        context=context,
        symbol=config.SYMBOL,
        timeframe=config.TIMEFRAME,
        strategy_version=snapshot.get("strategy_version"),
        testnet=bool(config.TESTNET),
        source="bot_runner_boot",
    )
    if not reconciliation.get("ok"):
        raise RuntimeError(f"Falha na reconciliacao live do runner: {reconciliation.get('error') or 'erro desconhecido'}")

    open_positions = _find_live_positions(context)
    max_open_real_trades = int(getattr(config, "MAX_OPEN_REAL_TRADES", 1) or 1)
    if len(open_positions) > max_open_real_trades:
        raise RuntimeError(
            f"Exchange retornou {len(open_positions)} posicoes abertas, acima do limite de runtime ({max_open_real_trades})."
        )

    if recovered_position is None and open_positions:
        raise RuntimeError(
            "Exchange possui posicao aberta, mas o runtime local nao restaurou estado. "
            "Sincronize a posicao antes de religar o bot."
        )

    if recovered_position is not None and not open_positions:
        log_info("Aviso: estado local indicava posicao aberta, mas a exchange nao possui posicao. Limpando recovery local.")
        try:
            db.save_user_execution_event(
                {
                    "user_id": int(context["user_id"]),
                    "account_id": str(context["account_id"]),
                    "exchange": context.get("exchange_name"),
                    "symbol": config.SYMBOL,
                    "timeframe": config.TIMEFRAME,
                    "strategy_version": snapshot.get("strategy_version"),
                    "event_type": "runtime_recovery_reset",
                    "event_status": "warning",
                    "message": "Recovery local limpo porque a exchange nao possui posicao aberta.",
                    "details_json": {"source": "bot_runner_boot"},
                }
            )
        except Exception:
            pass
        recovered_position = None

    if recovered_position is not None and open_positions:
        live_position = open_positions[0]
        live_side = str(live_position.get("side") or "").strip().lower()
        runtime_side = str(recovered_position.get("side") or "").strip().lower()
        if live_side and runtime_side and live_side != runtime_side:
            raise RuntimeError(
                f"Recovery inconsistente: runtime={runtime_side} e exchange={live_side}. "
                "Ajuste a posicao manualmente antes de continuar."
            )
        recovered_position["quantity"] = float(
            recovered_position.get("quantity") or live_position.get("quantity") or 0.0
        )
        recovered_position["execution_mode"] = "live"
        recovered_position["exchange_position_side"] = live_side or runtime_side
        if float(recovered_position.get("quantity") or 0.0) <= 0:
            raise RuntimeError("Posicao live recuperada sem quantity valida para fechamento reduce-only.")
        if _resolve_position_execution_profile(recovered_position) != "native_bracket":
            try:
                open_orders = db.get_user_live_orders(
                    user_id=int(context["user_id"]),
                    account_id=str(context["account_id"]),
                )
                active_stop_orders = [
                    row
                    for row in (open_orders or [])
                    if str(row.get("symbol") or "").strip().upper() == str(config.SYMBOL).strip().upper()
                    and str(row.get("order_type") or "").strip().lower() == "stop_market"
                    and str(row.get("status") or "").strip().lower() in {"open", "new", "pending", "partially_filled"}
                ]
                if active_stop_orders:
                    recovered_position["protective_stop_order_id"] = active_stop_orders[0].get("exchange_order_id")
                    recovered_position["protective_stop_client_order_id"] = active_stop_orders[0].get("client_order_id")
                    recovered_position["protective_stop_price"] = float(
                        recovered_position.get("current_stop") or 0.0
                    )
                else:
                    restored_stop = execution_service.submit_stop_market_order(
                        context=context,
                        symbol=config.SYMBOL,
                        side=_opposite_signal_for_position(recovered_position),
                        stop_price=float(recovered_position.get("current_stop") or 0.0),
                        quantity=float(recovered_position.get("quantity") or 0.0),
                        testnet=bool(config.TESTNET),
                        metadata={"reason": "startup_stop_restore"},
                    )
                    recovered_position["protective_stop_order_id"] = restored_stop.get("exchange_order_id")
                    recovered_position["protective_stop_client_order_id"] = restored_stop.get("client_order_id")
                    recovered_position["protective_stop_price"] = float(
                        restored_stop.get("stop_price") or recovered_position.get("current_stop") or 0.0
                    )
                    recovered_position["last_stop_sync_at"] = datetime.now(UTC).isoformat()
                    log_info("Stop live gerenciado restaurado no startup.")
            except Exception as stop_recovery_error:
                log_info(f"Aviso: falha ao validar/restaurar stop live no startup: {stop_recovery_error}")

    user_stream = execution_service.start_user_data_stream(
        context=context,
        symbol=config.SYMBOL,
        timeframe=config.TIMEFRAME,
        strategy_version=snapshot.get("strategy_version"),
        testnet=bool(config.TESTNET),
    )
    if hasattr(user_stream, "wait_until_ready") and not user_stream.wait_until_ready(timeout=15.0):
        raise RuntimeError("User data stream nao ficou pronto dentro da janela de startup.")

    env_label = "TESTNET" if bool(config.TESTNET) else "CONTA REAL"
    log_info(
        "Camada live do runner ativa | "
        f"modo={env_label} | "
        f"account={context['account_id']} | "
        f"exchange={context['exchange_name']} | "
        f"positions_open={len(open_positions)}"
    )
    return context, recovered_position, user_stream


def _serialize_position(position: dict | None) -> dict | None:
    if position is None:
        return None
    return {
        "side": position.get("side"),
        "entry_price": position.get("entry_price"),
        "entry_timestamp": str(position.get("entry_timestamp")),
        "best_price": position.get("best_price"),
        "initial_stop": position.get("initial_stop"),
        "current_stop": position.get("current_stop"),
        "partial_target": position.get("partial_target"),
        "trailing_trigger_price": position.get("trailing_trigger_price"),
        "trailing_trigger_pct": position.get("trailing_trigger_pct"),
        "trailing_stop_pct": position.get("trailing_stop_pct"),
        "partial_taken": position.get("partial_taken"),
        "break_even_active": position.get("break_even_active"),
        "realized_partial_pct": position.get("realized_partial_pct"),
        "atr": position.get("atr"),
        "stop_loss_pct": position.get("stop_loss_pct"),
        "partial_target_pct": position.get("partial_target_pct"),
        "management_profile": position.get("management_profile"),
        "mfe_pct": position.get("mfe_pct"),
        "mae_pct": position.get("mae_pct"),
        "max_unrealized_rr": position.get("max_unrealized_rr"),
        "quantity": position.get("quantity"),
        "account_reference_balance": position.get("account_reference_balance"),
        "planned_position_notional": position.get("planned_position_notional"),
        "risk_amount": position.get("risk_amount"),
        "execution_mode": position.get("execution_mode"),
        "execution_profile": position.get("execution_profile"),
        "strategy_version": position.get("strategy_version"),
        "client_order_id": position.get("client_order_id"),
        "exchange_order_id": position.get("exchange_order_id"),
        "exchange_position_side": position.get("exchange_position_side"),
        "entry_fill_price_source": position.get("entry_fill_price_source"),
        "protective_stop_order_id": position.get("protective_stop_order_id"),
        "protective_stop_client_order_id": position.get("protective_stop_client_order_id"),
        "protective_stop_price": position.get("protective_stop_price"),
        "last_stop_sync_at": position.get("last_stop_sync_at"),
        "live_partial_realized_pct_accounted": position.get("live_partial_realized_pct_accounted"),
        "paper_trade_id": position.get("paper_trade_id"),
        "entry_signal_reason": position.get("entry_signal_reason"),
        "entry_setup": position.get("entry_setup"),
        "entry_source_setup": position.get("entry_source_setup"),
        "entry_regime": position.get("entry_regime"),
        "signal_timestamp": position.get("signal_timestamp"),
        "signal_hour_utc": position.get("signal_hour_utc"),
        "signal_rsi": position.get("signal_rsi"),
        "signal_adx": position.get("signal_adx"),
        "signal_atr_pct": position.get("signal_atr_pct"),
        "signal_trend_strength_pct": position.get("signal_trend_strength_pct"),
        "signal_context_gap_pct": position.get("signal_context_gap_pct"),
        "pending_live_protection_action": position.get("pending_live_protection_action"),
        "pending_live_protection_reason": position.get("pending_live_protection_reason"),
        "pending_live_protection_since": position.get("pending_live_protection_since"),
        "pending_live_protection_attempts": position.get("pending_live_protection_attempts"),
        "pending_live_protection_last_error": position.get("pending_live_protection_last_error"),
        "pending_live_protection_stop_price": position.get("pending_live_protection_stop_price"),
        "pending_live_protection_quantity_after": position.get("pending_live_protection_quantity_after"),
        "pending_live_partial_quantity": position.get("pending_live_partial_quantity"),
        "pending_live_realized_partial_pct": position.get("pending_live_realized_partial_pct"),
    }


def _serialize_signal_result(signal_result: dict | None) -> dict:
    if not signal_result:
        return {}

    setup_payload = signal_result.get("setup") or {}
    regime_payload = setup_payload.get("regime") if isinstance(setup_payload, dict) else {}
    ai_payload = signal_result.get("ai_decision") or {}
    return {
        "signal": signal_result.get("signal"),
        "reason": signal_result.get("reason"),
        "score": signal_result.get("score"),
        "atr": signal_result.get("atr"),
        "decision_source": signal_result.get("decision_source"),
        "setup_name": setup_payload.get("setup") if isinstance(setup_payload, dict) else None,
        "direction": setup_payload.get("direction") if isinstance(setup_payload, dict) else None,
        "regime_name": regime_payload.get("regime") if isinstance(regime_payload, dict) else None,
        "ai_confidence": ai_payload.get("confidence"),
        "ai_signal": ai_payload.get("signal"),
        "fear_greed_value": ai_payload.get("fear_greed_value"),
        "news_sentiment_score": ai_payload.get("news_sentiment_score"),
    }


def _serialize_runtime_session(runtime_session: dict | None) -> dict:
    if not runtime_session:
        return {}
    return {
        key: value
        for key, value in runtime_session.items()
        if key not in {"started_at_epoch"}
    }


def _persist_runtime_state(
    *,
    snapshot: dict,
    status: str,
    timestamp_value=None,
    signal: dict | None = None,
    position: dict | None = None,
    risk_state: dict | None = None,
    last_error: str | None = None,
    last_price: float | None = None,
    runtime_session: dict | None = None,
) -> None:
    payload = {
        "runtime_key": _runtime_key(),
        "runtime_name": "main_bot",
        "environment": "testnet" if bool(config.TESTNET) else "mainnet",
        "symbol": config.SYMBOL,
        "timeframe": config.TIMEFRAME,
        "strategy_version": snapshot.get("strategy_version"),
        "status": status,
        "last_heartbeat_at": datetime.now(UTC).isoformat(),
        "last_candle_timestamp": None if timestamp_value is None else str(timestamp_value),
        "last_signal": None if signal is None else signal.get("signal"),
        "last_signal_reason": None if signal is None else signal.get("reason"),
        "last_signal_price": last_price,
        "position_side": None if position is None else position.get("side"),
        "position_entry_price": None if position is None else position.get("entry_price"),
        "blocked": bool((risk_state or {}).get("blocked", False)),
        "block_reason": (risk_state or {}).get("block_reason"),
        "last_error": last_error,
        "state_payload": {
            "snapshot": snapshot,
            "risk_state": risk_state or {},
            "position_open": bool(position is not None),
            "position": _serialize_position(position),
            "last_signal_details": _serialize_signal_result(signal),
            "entry_runtime": _serialize_runtime_session(runtime_session),
        },
    }
    try:
        db.upsert_bot_runtime_state(payload)
    except Exception as exc:
        log_info(f"Aviso: falha ao persistir estado do runtime: {exc}")


def _persist_backtest_websocket_frame(df, *, source: str = "bot_runner_public_websocket") -> int:
    if df is None or getattr(df, "empty", True):
        return 0

    required_columns = ["timestamp", "open", "high", "low", "close", "volume"]
    for column in required_columns:
        if column not in df.columns:
            return 0

    try:
        working_df = df[required_columns].copy()
        candle_rows = working_df.to_dict("records")
        return int(
            db.store_backtest_websocket_candles(
                symbol=config.SYMBOL,
                timeframe=config.TIMEFRAME,
                candles=candle_rows,
                source=source,
            )
            or 0
        )
    except Exception as exc:
        log_info(f"Aviso: falha ao persistir candles websocket para backtest: {exc}")
        return 0


def _runtime_paper_fee_rate() -> float:
    return max(float(config.FEE_PCT or 0.0), 0.0) / 100.0


def _runtime_paper_close_result(position: dict, closed_trade: dict) -> dict:
    entry_price = float(position.get("entry_price") or 0.0)
    exit_price = float(closed_trade.get("exit_price") or 0.0)
    fee_rate = _runtime_paper_fee_rate()
    if closed_trade.get("gross_pct") not in (None, ""):
        gross_result_pct = float(closed_trade.get("gross_pct") or 0.0)
    elif position.get("side") == "long":
        gross_result_pct = ((exit_price - entry_price) / entry_price) * 100 if entry_price > 0 else 0.0
    else:
        gross_result_pct = ((entry_price - exit_price) / entry_price) * 100 if entry_price > 0 else 0.0

    entry_fee_pct = fee_rate * 100
    exit_fee_pct = ((exit_price / entry_price) * fee_rate * 100) if entry_price > 0 else fee_rate * 100
    result_pct = gross_result_pct - entry_fee_pct - exit_fee_pct
    if result_pct > 0:
        outcome = "WIN"
    elif result_pct < 0:
        outcome = "LOSS"
    else:
        outcome = "FLAT"
    planned_position_notional = float(position.get("planned_position_notional", 0.0) or 0.0)
    result_usdt = planned_position_notional * (float(result_pct) / 100.0)

    return {
        "outcome": outcome,
        "result_pct": round(float(result_pct), 4),
        "result_usdt": round(float(result_usdt), 4),
        "exit_timestamp": str(closed_trade.get("exit_timestamp")),
        "exit_price": round(exit_price, 6),
    }


def _resolve_runtime_paper_account_balance(runtime_snapshot: dict | None = None) -> float:
    strategy_version = None if runtime_snapshot is None else runtime_snapshot.get("strategy_version")
    try:
        drawdown_summary = db.get_paper_drawdown_summary(
            symbol=config.SYMBOL,
            timeframe=config.TIMEFRAME,
            strategy_version=strategy_version,
        )
        current_equity = float(drawdown_summary.get("current_equity", 0.0) or 0.0)
        if current_equity > 0:
            return current_equity
    except Exception as exc:
        log_info(f"Aviso: falha ao resolver saldo de referencia do paper runtime: {exc}")
    return float(getattr(config.ProductionConfig, "PAPER_ACCOUNT_BALANCE", 10000.0) or 10000.0)


def _build_runtime_paper_position_metrics(position: dict, runtime_snapshot: dict | None = None) -> dict:
    if not position:
        return {
            "planned_risk_pct": float(getattr(config, "RISK_PER_TRADE_PCT", 0.0) or 0.0),
            "risk_amount": 0.0,
            "planned_position_notional": 0.0,
            "quantity": 0.0,
            "planned_quantity": 0.0,
            "account_reference_balance": _resolve_runtime_paper_account_balance(runtime_snapshot),
            "risk_mode": "normal",
            "size_reduced": False,
            "risk_reason": "",
        }

    risk_service = RiskManagementService(database=db)
    account_balance = _resolve_runtime_paper_account_balance(runtime_snapshot)
    entry_price = float(position.get("entry_price", 0.0) or 0.0)
    stop_loss_pct = _resolve_runtime_position_stop_pct(position)
    risk_pct = float(getattr(config, "RISK_PER_TRADE_PCT", 0.0) or 0.0)
    sizing = risk_service.calculate_position_size(
        account_balance=account_balance,
        entry_price=entry_price,
        stop_loss_pct=stop_loss_pct,
        risk_pct=risk_pct,
    )
    quantity = float(sizing.get("quantity", 0.0) or 0.0)
    position_notional = float(sizing.get("position_notional", 0.0) or 0.0)
    risk_amount = float(sizing.get("risk_amount", 0.0) or 0.0)
    return {
        "planned_risk_pct": risk_pct,
        "risk_amount": risk_amount,
        "planned_position_notional": position_notional,
        "quantity": quantity,
        "planned_quantity": quantity,
        "account_reference_balance": float(account_balance),
        "risk_mode": "normal",
        "size_reduced": False,
        "risk_reason": "",
    }


def _create_runtime_paper_trade(position: dict, signal_result: dict, runtime_snapshot: dict) -> int | None:
    if not _paper_tracking_enabled():
        return None

    side = str(position.get("side") or "").strip().lower()
    stop_loss_pct = float(config.LONG_STOP_LOSS_PCT if side == "long" else config.SHORT_STOP_LOSS_PCT)
    take_profit_pct = float(config.LONG_TAKE_PROFIT_PCT if side == "long" else config.SHORT_TAKE_PROFIT_PCT)
    signal_name = "COMPRA" if side == "long" else "VENDA"
    setup_payload = signal_result.get("setup") or {}
    setup_name = (
        setup_payload.get("setup")
        if isinstance(setup_payload, dict)
        else setup_payload
    ) or runtime_snapshot.get("strategy_version")
    regime_name = None
    if isinstance(setup_payload, dict):
        regime_payload = setup_payload.get("regime") or {}
        if isinstance(regime_payload, dict):
            regime_name = regime_payload.get("regime")
    try:
        return db.create_paper_trade(
            {
                "symbol": config.SYMBOL,
                "timeframe": config.TIMEFRAME,
                "context_timeframe": runtime_snapshot.get("context_timeframe"),
                "setup_name": setup_name,
                "strategy_version": runtime_snapshot.get("strategy_version"),
                "execution_mode": "runtime_testnet" if bool(config.TESTNET) else "runtime_paper",
                "regime": regime_name,
                "signal_score": signal_result.get("score", 0.0),
                "atr": signal_result.get("atr", position.get("atr", 0.0)),
                "sample_type": "testnet_runtime" if bool(config.TESTNET) else "paper_runtime",
                "signal": signal_name,
                "side": side,
                "source": "bot_runner",
                "entry_timestamp": str(position.get("entry_timestamp")),
                "entry_reason": signal_result.get("reason") or signal_name,
                "entry_price": float(position.get("entry_price") or 0.0),
                "stop_loss_pct": stop_loss_pct,
                "take_profit_pct": take_profit_pct,
                "fee_rate": _runtime_paper_fee_rate(),
                "slippage": 0.0,
                "stop_loss_price": position.get("current_stop"),
                "take_profit_price": position.get("partial_target"),
                "initial_stop_price": position.get("initial_stop"),
                "initial_take_price": position.get("partial_target"),
                "final_stop_price": position.get("current_stop"),
                "final_take_price": position.get("partial_target"),
                "break_even_active": bool(position.get("break_even_active", False)),
                "trailing_active": _derive_runtime_trailing_active(position),
                "protection_level": "elevated" if _derive_runtime_trailing_active(position) else "normal",
                "mfe_pct": 0.0,
                "mae_pct": 0.0,
                "max_unrealized_rr": 0.0,
                "planned_risk_pct": float(position.get("planned_risk_pct", 0.0) or 0.0),
                "planned_risk_amount": float(position.get("risk_amount", 0.0) or 0.0),
                "planned_position_notional": float(position.get("planned_position_notional", 0.0) or 0.0),
                "planned_quantity": float(
                    position.get("planned_quantity", position.get("quantity", 0.0)) or 0.0
                ),
                "account_reference_balance": float(position.get("account_reference_balance", 0.0) or 0.0),
                "risk_mode": str(position.get("risk_mode") or "normal"),
                "size_reduced": bool(position.get("size_reduced", False)),
                "risk_reason": position.get("risk_reason"),
                "status": "OPEN",
                "outcome": "OPEN",
                "result_pct": 0.0,
            }
        )
    except Exception as exc:
        log_info(f"Aviso: falha ao criar paper trade do runtime: {exc}")
        return None


def _update_runtime_paper_trade(position: dict) -> None:
    trade_id = position.get("paper_trade_id")
    if not _paper_tracking_enabled() or trade_id in (None, ""):
        return
    try:
        db.update_paper_trade_management(
            trade_id=int(trade_id),
            stop_loss_price=position.get("current_stop"),
            take_profit_price=position.get("partial_target"),
            break_even_active=bool(position.get("break_even_active", False)),
            trailing_active=_derive_runtime_trailing_active(position),
            protection_level="elevated"
            if bool(position.get("break_even_active", False)) or _derive_runtime_trailing_active(position)
            else "normal",
            mfe_pct=float(position.get("mfe_pct", 0.0) or 0.0),
            mae_pct=float(position.get("mae_pct", 0.0) or 0.0),
            max_unrealized_rr=float(position.get("max_unrealized_rr", 0.0) or 0.0),
        )
    except Exception as exc:
        log_info(f"Aviso: falha ao atualizar paper trade do runtime: {exc}")


def _close_runtime_paper_trade(position: dict, closed_trade: dict) -> None:
    trade_id = position.get("paper_trade_id")
    if not _paper_tracking_enabled() or trade_id in (None, ""):
        return

    close_result = _runtime_paper_close_result(position, closed_trade)
    try:
        db.close_paper_trade(
            trade_id=int(trade_id),
            exit_timestamp=close_result["exit_timestamp"],
            exit_price=close_result["exit_price"],
            outcome=close_result["outcome"],
            close_reason=str(closed_trade.get("reason") or "runtime_exit").upper(),
            result_pct=close_result["result_pct"],
            final_stop_price=position.get("current_stop"),
            final_take_price=position.get("partial_target"),
            break_even_active=bool(position.get("break_even_active", False)),
            trailing_active=_derive_runtime_trailing_active(position),
            protection_level="elevated"
            if bool(position.get("break_even_active", False)) or _derive_runtime_trailing_active(position)
            else "normal",
            mfe_pct=float(position.get("mfe_pct", 0.0) or 0.0),
            mae_pct=float(position.get("mae_pct", 0.0) or 0.0),
            max_unrealized_rr=float(position.get("max_unrealized_rr", 0.0) or 0.0),
        )
    except Exception as exc:
        log_info(f"Aviso: falha ao fechar paper trade do runtime: {exc}")
    return close_result


def _execute_ai_forced_exit(
    *,
    position_before_close: dict,
    candle_row,
    risk_state: dict,
    timestamp_atual,
    runtime_snapshot: dict,
    runtime_session: dict,
    live_execution_service,
    live_execution_context,
    close_reason: str,
    unified_decision_engine: UnifiedDecisionEngine | None = None,
) -> dict | None:
    realized_partial_pct = float(position_before_close.get("realized_partial_pct", 0.0) or 0.0)
    accounted_partial_pct = float(position_before_close.get("live_partial_realized_pct_accounted", 0.0) or 0.0)
    trade = _finalize_runtime_managed_trade(
        position_before_close,
        _build_forced_runtime_close(position_before_close, candle_row, reason=close_reason),
        realized_partial_pct,
    )
    gross_pct_contribution = float(trade.get("gross_pct", 0.0) or 0.0) - accounted_partial_pct

    if _live_execution_enabled():
        try:
            exit_quantity = float(position_before_close.get("quantity", 0.0) or 0.0)
            if exit_quantity <= 0:
                raise RuntimeError("Posicao live sem quantity valida para fechamento por IA.")
            exit_result = live_execution_service.submit_market_order(
                context=live_execution_context,
                symbol=config.SYMBOL,
                timeframe=config.TIMEFRAME,
                strategy_version=runtime_snapshot.get("strategy_version"),
                signal_side=_opposite_signal_for_position(position_before_close),
                quantity=exit_quantity,
                reduce_only=True,
                source="bot_runner_ai_exit",
                testnet=bool(config.TESTNET),
                leverage=int(getattr(config, "LEVERAGE", 1) or 1),
                metadata={
                    "reason": close_reason,
                    "entry_price": position_before_close.get("entry_price"),
                    "best_price": position_before_close.get("best_price"),
                },
            )
            exit_fill_price = float(exit_result.get("price") or 0.0)
            if exit_fill_price > 0:
                adjusted_trade = _build_forced_runtime_close(position_before_close, candle_row, reason=close_reason)
                adjusted_trade["exit_price"] = exit_fill_price
                adjusted_trade["gross_pct"] = _calculate_trade_pct_from_position(position_before_close, exit_fill_price)
                trade = _finalize_runtime_managed_trade(
                    position_before_close,
                    adjusted_trade,
                    realized_partial_pct,
                )
                gross_pct_contribution = float(trade.get("gross_pct", 0.0) or 0.0) - accounted_partial_pct

            try:
                live_execution_service.cancel_all_symbol_orders(
                    context=live_execution_context,
                    symbol=config.SYMBOL,
                )
            except Exception as cancel_exc:
                log_info(f"Aviso: falha ao limpar ordens nativas apos saida IA: {cancel_exc}")

            log_info(
                "Saida IA live:",
                trade["reason"],
                "| resultado %:",
                round(trade["gross_pct"], 4),
                "| order:",
                exit_result.get("exchange_order_id"),
            )
            _save_live_realization_event(
                context=live_execution_context,
                position=position_before_close,
                trade=trade,
                gross_pct_contribution=gross_pct_contribution,
                event_type="live_trade_ai_exit",
                event_status="ok",
                message="Saida antecipada executada pela IA.",
                source="bot_runner_ai_exit",
            )
        except Exception as live_exit_error:
            log_info(f"Falha ao enviar saida IA live: {live_exit_error}")
            return position_before_close
    else:
        close_result = _close_runtime_paper_trade(position_before_close, trade)
        if close_result is not None:
            log_info(
                f"Saida IA: {trade['reason']} | resultado %: {float(close_result['result_pct']):.4f} | "
                f"pnl_usdt: {float(close_result['result_usdt']):.2f}"
            )
        else:
            log_info(f"Saida IA: {trade['reason']} | resultado %: {round(trade['gross_pct'], 4)}")

    _update_risk_circuit_breaker(
        risk_state,
        {"gross_pct": gross_pct_contribution, "reason": trade.get("reason")},
        timestamp_atual,
    )
    runtime_session["last_ai_exit"] = {
        "closed_at_utc": datetime.now(UTC).isoformat(),
        "candle_timestamp": str(timestamp_atual),
        "side": position_before_close.get("side"),
        "entry_price": float(position_before_close.get("entry_price") or 0.0),
        "reason": close_reason,
        "gross_pct": float(trade.get("gross_pct", 0.0) or 0.0),
    }
    if unified_decision_engine is not None:
        unified_decision_engine.register_trade_outcome(trade)
    return None


def _attach_runtime_open_paper_trade(position: dict | None, runtime_snapshot: dict) -> dict | None:
    if not _paper_tracking_enabled() or position is None or position.get("paper_trade_id") not in (None, ""):
        return position
    try:
        open_trades = db.get_open_paper_trades(
            symbol=config.SYMBOL,
            timeframe=config.TIMEFRAME,
            strategy_version=runtime_snapshot.get("strategy_version"),
        )
        if len(open_trades) == 1:
            position["paper_trade_id"] = int(open_trades[0]["id"])
    except Exception as exc:
        log_info(f"Aviso: falha ao reconciliar paper trade aberto do runtime: {exc}")
    return position


def _parse_runtime_timestamp(raw_value):
    if raw_value in (None, ""):
        return None
    if isinstance(raw_value, datetime):
        return raw_value
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return raw_value


def _normalize_risk_state(raw_state: dict | None) -> dict:
    source = raw_state or {}
    return {
        "day": source.get("day"),
        "daily_realized_pct": float(source.get("daily_realized_pct", 0.0) or 0.0),
        "consecutive_losses": int(source.get("consecutive_losses", 0) or 0),
        "blocked": bool(source.get("blocked", False)),
        "block_reason": str(source.get("block_reason") or ""),
    }


def _restore_position(raw_position: dict | None) -> dict | None:
    if not raw_position:
        return None

    restored = dict(raw_position)
    for key in (
        "entry_price",
        "best_price",
        "initial_stop",
        "current_stop",
        "partial_target",
        "trailing_trigger_price",
        "trailing_trigger_pct",
        "trailing_stop_pct",
        "stop_loss_pct",
        "partial_target_pct",
        "realized_partial_pct",
        "atr",
        "mfe_pct",
        "mae_pct",
        "max_unrealized_rr",
        "quantity",
        "account_reference_balance",
        "planned_position_notional",
        "risk_amount",
        "protective_stop_price",
        "live_partial_realized_pct_accounted",
        "signal_rsi",
        "signal_adx",
        "signal_atr_pct",
        "signal_trend_strength_pct",
        "signal_context_gap_pct",
        "pending_live_protection_stop_price",
        "pending_live_protection_quantity_after",
        "pending_live_partial_quantity",
        "pending_live_realized_partial_pct",
    ):
        if restored.get(key) not in (None, ""):
            restored[key] = float(restored[key])
    if restored.get("pending_live_protection_attempts") not in (None, ""):
        try:
            restored["pending_live_protection_attempts"] = int(restored["pending_live_protection_attempts"])
        except (TypeError, ValueError):
            restored["pending_live_protection_attempts"] = 0
    if restored.get("signal_hour_utc") not in (None, ""):
        try:
            restored["signal_hour_utc"] = int(restored["signal_hour_utc"])
        except (TypeError, ValueError):
            restored["signal_hour_utc"] = None
    restored["execution_profile"] = _resolve_position_execution_profile(restored)
    restored["partial_taken"] = bool(restored.get("partial_taken", False))
    restored["break_even_active"] = bool(restored.get("break_even_active", False))
    restored["live_partial_realized_pct_accounted"] = float(
        restored.get("live_partial_realized_pct_accounted", 0.0) or 0.0
    )

    legacy_profile = not str(raw_position.get("execution_profile") or "").strip()
    if restored["execution_profile"] == "native_bracket" and legacy_profile:
        native_seed = create_native_bracket_position(
            "buy" if restored.get("side") == "long" else "sell",
            float(restored.get("entry_price") or 0.0),
            restored.get("entry_timestamp"),
            atr=float(restored.get("atr") or 0.0),
        )
        restored["initial_stop"] = float(native_seed["initial_stop"])
        restored["current_stop"] = float(native_seed["current_stop"])
        restored["partial_target"] = float(native_seed["partial_target"])
        restored["partial_taken"] = False
        restored["break_even_active"] = False
        if restored.get("best_price") in (None, ""):
            restored["best_price"] = float(native_seed["best_price"])
    elif restored["execution_profile"] == "managed":
        managed_seed = _build_runtime_position(
            signal="buy" if restored.get("side") == "long" else "sell",
            entry_price=float(restored.get("entry_price") or 0.0),
            timestamp=restored.get("entry_timestamp"),
            atr=float(restored.get("atr") or 0.0),
            execution_profile="managed",
            entry_setup=restored.get("entry_setup"),
            entry_source_setup=restored.get("entry_source_setup"),
        )
        if restored.get("initial_stop") in (None, ""):
            restored["initial_stop"] = float(managed_seed.get("initial_stop") or 0.0)
        if restored.get("current_stop") in (None, ""):
            restored["current_stop"] = float(managed_seed.get("current_stop") or 0.0)
        if restored.get("partial_target") in (None, ""):
            restored["partial_target"] = float(managed_seed.get("partial_target") or 0.0)
        if restored.get("trailing_trigger_price") in (None, ""):
            restored["trailing_trigger_price"] = float(managed_seed.get("trailing_trigger_price") or 0.0)
        if restored.get("trailing_trigger_pct") in (None, ""):
            restored["trailing_trigger_pct"] = float(managed_seed.get("trailing_trigger_pct") or 0.0)
        if restored.get("trailing_stop_pct") in (None, ""):
            restored["trailing_stop_pct"] = float(managed_seed.get("trailing_stop_pct") or 0.0)
        if restored.get("stop_loss_pct") in (None, ""):
            restored["stop_loss_pct"] = float(managed_seed.get("stop_loss_pct") or 0.0)
        if restored.get("partial_target_pct") in (None, ""):
            restored["partial_target_pct"] = float(managed_seed.get("partial_target_pct") or 0.0)
        if not str(restored.get("management_profile") or "").strip():
            restored["management_profile"] = managed_seed.get("management_profile")
        if restored.get("best_price") in (None, ""):
            restored["best_price"] = float(managed_seed.get("best_price") or restored.get("entry_price") or 0.0)
        if restored.get("realized_partial_pct") in (None, ""):
            if bool(restored.get("partial_taken", False)):
                entry_price = float(restored.get("entry_price") or 0.0)
                partial_target = float(restored.get("partial_target") or 0.0)
                if entry_price > 0 and partial_target > 0:
                    if restored.get("side") == "long":
                        restored["realized_partial_pct"] = ((partial_target - entry_price) / entry_price * 100) * 0.5
                    else:
                        restored["realized_partial_pct"] = ((entry_price - partial_target) / entry_price * 100) * 0.5
                else:
                    restored["realized_partial_pct"] = 0.0
            else:
                restored["realized_partial_pct"] = 0.0
    return restored


def _load_runtime_recovery_state(snapshot: dict) -> tuple[object, dict | None, dict]:
    risk_state = {
        "day": None,
        "daily_realized_pct": 0.0,
        "consecutive_losses": 0,
        "blocked": False,
        "block_reason": "",
    }
    rows = db.get_bot_runtime_state(runtime_key=_runtime_key(), limit=1)
    if not rows:
        return None, None, risk_state

    latest = rows[0]
    payload = latest.get("state_payload") or {}
    restored_position = _restore_position(payload.get("position"))
    restored_risk_state = _normalize_risk_state(payload.get("risk_state"))
    restored_timestamp = _parse_runtime_timestamp(latest.get("last_candle_timestamp"))

    previous_version = str(latest.get("strategy_version") or "")
    current_version = str(snapshot.get("strategy_version") or "")
    version_label = "igual" if previous_version == current_version else "diferente"
    log_info(
        "Recovery state | "
        f"status={latest.get('status')} | "
        f"last_candle={latest.get('last_candle_timestamp')} | "
        f"position={'yes' if restored_position else 'no'} | "
        f"strategy_version={version_label}"
    )

    return restored_timestamp, restored_position, restored_risk_state


def _roll_daily_state(state: dict, timestamp_value) -> None:
    day = _resolve_day(timestamp_value)
    if state["day"] != day:
        state["day"] = day
        state["daily_realized_pct"] = 0.0
        state["blocked"] = False
        state["block_reason"] = ""
        log_info(f"Novo dia operacional: {day} | limites diarios resetados.")


def _update_risk_circuit_breaker(state: dict, trade: dict, timestamp_value) -> None:
    _roll_daily_state(state, timestamp_value)
    result_pct = float(trade.get("gross_pct", 0.0) or 0.0)
    state["daily_realized_pct"] += result_pct

    if result_pct < 0:
        state["consecutive_losses"] += 1
    else:
        state["consecutive_losses"] = 0

    log_info(
        "Risco sessao | "
        f"daily_realized_pct={state['daily_realized_pct']:.4f}% | "
        f"consecutive_losses={state['consecutive_losses']}"
    )

    if bool(config.TESTNET):
        return

    max_daily_loss = float(getattr(config, "MAX_DAILY_REAL_LOSS_PCT", 2.5) or 2.5)
    max_consecutive_losses = int(getattr(config, "MAX_CONSECUTIVE_REAL_LOSSES", 4) or 4)

    if state["daily_realized_pct"] <= -abs(max_daily_loss):
        state["blocked"] = True
        state["block_reason"] = (
            f"Circuit breaker diario: perda {state['daily_realized_pct']:.4f}% "
            f"(limite -{abs(max_daily_loss):.2f}%)."
        )
        log_info(f"Bloqueio de seguranca: {state['block_reason']}")
        return

    if state["consecutive_losses"] >= max_consecutive_losses:
        state["blocked"] = True
        state["block_reason"] = (
            f"Circuit breaker por sequencia: {state['consecutive_losses']} perdas "
            f"(limite {max_consecutive_losses})."
        )
        log_info(f"Bloqueio de seguranca: {state['block_reason']}")


def _entry_allowed(state: dict) -> tuple[bool, str]:
    if state.get("blocked"):
        return False, str(state.get("block_reason") or "Runtime bloqueado por seguranca.")
    return True, ""


def _register_runtime_reentry_cooldown(
    runtime_session: dict,
    position: dict | None,
    timestamp_value,
    *,
    reason: str = "position_closed",
) -> None:
    if not position:
        return
    cooldown_candles = max(int(getattr(config, "BOT_REENTRY_COOLDOWN_CANDLES", 1) or 0), 0)
    if cooldown_candles <= 0:
        return
    side = str(position.get("side") or "").strip().lower()
    if side not in {"long", "short"}:
        return
    runtime_session["reentry_cooldown"] = {
        "side": side,
        "closed_candle_timestamp": str(timestamp_value),
        "created_processed_candles": int(runtime_session.get("processed_candles", 0) or 0),
        "cooldown_candles": cooldown_candles,
        "reason": reason,
    }


def _same_side_reentry_block_reason(runtime_session: dict, signal_side: str, timestamp_value) -> str:
    cooldown = runtime_session.get("reentry_cooldown") or {}
    if not isinstance(cooldown, dict) or not cooldown:
        return ""

    blocked_side = str(cooldown.get("side") or "").strip().lower()
    signal_position_side = _signal_to_position_side(signal_side)
    if blocked_side != signal_position_side:
        return ""

    created_processed = int(cooldown.get("created_processed_candles", 0) or 0)
    current_processed = int(runtime_session.get("processed_candles", 0) or 0)
    cooldown_candles = max(int(cooldown.get("cooldown_candles", 0) or 0), 0)
    candles_after_close = max(current_processed - created_processed, 0)
    same_close_candle = str(cooldown.get("closed_candle_timestamp") or "") == str(timestamp_value)
    if same_close_candle or candles_after_close <= cooldown_candles:
        return (
            "Cooldown de reentrada: posicao fechada recentemente na mesma direcao "
            f"({blocked_side}); aguardando novo candle de confirmacao."
        )

    runtime_session["reentry_cooldown"] = None
    return ""


def _mark_pending_live_protection(
    position: dict,
    *,
    action: str,
    reason: str,
    error: Exception | str,
    target_position: dict,
    partial_quantity: float = 0.0,
) -> dict:
    protected = dict(position)
    attempts = int(protected.get("pending_live_protection_attempts", 0) or 0) + 1
    protected["pending_live_protection_action"] = str(action or "").strip().lower()
    protected["pending_live_protection_reason"] = str(reason or "profit_protection")
    protected["pending_live_protection_since"] = (
        protected.get("pending_live_protection_since") or datetime.now(UTC).isoformat()
    )
    protected["pending_live_protection_attempts"] = attempts
    protected["pending_live_protection_last_error"] = str(error or "")
    protected["pending_live_protection_stop_price"] = float(target_position.get("current_stop") or 0.0)
    protected["pending_live_protection_quantity_after"] = float(target_position.get("quantity") or position.get("quantity") or 0.0)
    protected["pending_live_partial_quantity"] = float(partial_quantity or 0.0)
    protected["pending_live_realized_partial_pct"] = float(target_position.get("realized_partial_pct", 0.0) or 0.0)
    return protected


def _clear_pending_live_protection(position: dict) -> dict:
    cleared = dict(position)
    for key in (
        "pending_live_protection_action",
        "pending_live_protection_reason",
        "pending_live_protection_since",
        "pending_live_protection_attempts",
        "pending_live_protection_last_error",
        "pending_live_protection_stop_price",
        "pending_live_protection_quantity_after",
        "pending_live_partial_quantity",
        "pending_live_realized_partial_pct",
    ):
        cleared.pop(key, None)
    return cleared


def _retry_pending_live_protection(
    *,
    position: dict,
    runtime_snapshot: dict,
    live_execution_service,
    live_execution_context,
) -> dict:
    action = str(position.get("pending_live_protection_action") or "").strip().lower()
    if not action:
        return position

    retry_position = dict(position)
    target_stop = float(retry_position.get("pending_live_protection_stop_price") or retry_position.get("current_stop") or 0.0)
    target_quantity_after = float(
        retry_position.get("pending_live_protection_quantity_after")
        or retry_position.get("quantity")
        or 0.0
    )
    target_realized_partial_pct = float(
        retry_position.get("pending_live_realized_partial_pct")
        or retry_position.get("realized_partial_pct")
        or 0.0
    )
    target_position = dict(retry_position)
    target_position["current_stop"] = target_stop
    target_position["break_even_active"] = True
    target_position["realized_partial_pct"] = target_realized_partial_pct

    if action == "partial":
        partial_quantity = float(retry_position.get("pending_live_partial_quantity") or 0.0)
        if partial_quantity <= 0:
            partial_quantity = max(float(retry_position.get("quantity", 0.0) or 0.0) - target_quantity_after, 0.0)
        if partial_quantity <= 0:
            return _clear_pending_live_protection(retry_position)
        partial_result = live_execution_service.submit_market_order(
            context=live_execution_context,
            symbol=config.SYMBOL,
            timeframe=config.TIMEFRAME,
            strategy_version=runtime_snapshot.get("strategy_version"),
            signal_side=_opposite_signal_for_position(retry_position),
            quantity=partial_quantity,
            reduce_only=True,
            source="bot_runner_pending_partial_retry",
            testnet=bool(config.TESTNET),
            leverage=int(getattr(config, "LEVERAGE", 1) or 1),
            metadata={
                "reason": str(retry_position.get("pending_live_protection_reason") or "pending_partial_retry"),
                "attempt": int(retry_position.get("pending_live_protection_attempts", 0) or 0) + 1,
            },
        )
        filled_quantity = min(
            float(partial_result.get("quantity") or partial_quantity or 0.0),
            partial_quantity,
            float(retry_position.get("quantity", 0.0) or 0.0),
        )
        target_position["quantity"] = max(float(retry_position.get("quantity", 0.0) or 0.0) - filled_quantity, 0.0)
        target_position["partial_taken"] = True
        target_position["live_partial_realized_pct_accounted"] = target_realized_partial_pct
    else:
        target_position["quantity"] = float(retry_position.get("quantity", 0.0) or 0.0)

    synced_position = _refresh_live_managed_protective_stop(
        execution_service=live_execution_service,
        context=live_execution_context,
        position_before=retry_position,
        position_after=target_position,
    )
    return _clear_pending_live_protection(synced_position)


def _load_bootstrap_candles(bootstrap_limit: int | None = None):
    if not bool(getattr(config, "BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP", False)):
        log_info("Bootstrap CSV desativado; bot iniciara somente com dados da WebSocket.")
        return None

    resolved_bootstrap_limit = max(
        int(bootstrap_limit or 0),
        int(getattr(config, "BOT_BOOTSTRAP_CANDLES", config.LIMIT) or config.LIMIT),
        max(int(config.LIMIT), 200),
    )
    try:
        bootstrap_df = fetch_historical_candles_from_csv(
            config.SYMBOL,
            config.TIMEFRAME,
            total_limit=resolved_bootstrap_limit,
        )
        log_info(f"Bootstrap local carregado: {len(bootstrap_df)} candles.")
        return bootstrap_df
    except FileNotFoundError:
        if bool(getattr(config, "BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP", False)):
            log_info(
                "Aviso: BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP estava ativo, mas o runtime atual nao exige mais CSV obrigatorio. "
                "O bot iniciara apenas com buffer do websocket."
            )
        else:
            log_info("Aviso: CSV de bootstrap nao encontrado. Bot iniciara apenas com buffer do websocket.")
        return None


def _resolve_runtime_market_data_limit(params) -> int:
    indicator_warmup = int(get_min_required_rows(params)) + 25
    bootstrap_target = int(getattr(config, "BOT_BOOTSTRAP_CANDLES", config.LIMIT) or config.LIMIT)
    return max(int(config.LIMIT), indicator_warmup, bootstrap_target, 300)


def _validate_stream_runtime_ready(stream_status: dict) -> None:
    last_error = str(stream_status.get("last_error") or "").strip()
    if "websockets nao instalado" in last_error.lower():
        raise RuntimeError(
            "Runtime bloqueado: pacote websockets indisponivel no ambiente. "
            "O bot nao pode operar em modo constante sem streaming de candles."
        )


def _get_pending_candle_indexes(df, ultimo_timestamp) -> list[int]:
    if df is None or df.empty:
        return []

    if ultimo_timestamp is None:
        return []

    pending_indexes: list[int] = []
    timestamps = df["timestamp"].tolist()
    if "is_closed" in df.columns:
        closed_flags = df["is_closed"].fillna(False).astype(bool).tolist()
    else:
        closed_flags = [True] * len(timestamps)
    for idx, candle_timestamp in enumerate(timestamps):
        if not bool(closed_flags[idx]):
            continue
        if candle_timestamp > ultimo_timestamp:
            pending_indexes.append(idx)
    return pending_indexes


def _monitor_open_position_intrabar(
    *,
    df,
    posicao_atual,
    risk_state: dict,
    runtime_snapshot: dict,
    live_execution_service,
    live_execution_context,
    runtime_session: dict,
    unified_decision_engine: UnifiedDecisionEngine | None = None,
):
    if posicao_atual is None:
        return posicao_atual
    if not bool(getattr(config.ProductionConfig, "AI_INTRABAR_POSITION_MONITOR", True)):
        return posicao_atual
    if df is None or df.empty:
        return posicao_atual

    candle_slice = df.iloc[:]
    candle_row = candle_slice.iloc[-1]
    if bool(candle_row.get("is_closed", False)):
        return posicao_atual

    timestamp_atual = candle_row["timestamp"]
    price_now = float(candle_row["close"])

    if str(posicao_atual.get("pending_live_protection_action") or "").strip():
        try:
            retried_position = _retry_pending_live_protection(
                position=posicao_atual,
                runtime_snapshot=runtime_snapshot,
                live_execution_service=live_execution_service,
                live_execution_context=live_execution_context,
            )
            log_info("Protecao live pendente executada com sucesso.")
            return retried_position
        except Exception as retry_error:
            target_position = dict(posicao_atual)
            target_position["current_stop"] = float(
                posicao_atual.get("pending_live_protection_stop_price")
                or posicao_atual.get("current_stop")
                or 0.0
            )
            target_position["quantity"] = float(
                posicao_atual.get("pending_live_protection_quantity_after")
                or posicao_atual.get("quantity")
                or 0.0
            )
            target_position["realized_partial_pct"] = float(
                posicao_atual.get("pending_live_realized_partial_pct")
                or posicao_atual.get("realized_partial_pct")
                or 0.0
            )
            log_info(f"Aviso: protecao live pendente ainda falhou; nova tentativa no proximo ciclo: {retry_error}")
            return _mark_pending_live_protection(
                posicao_atual,
                action=str(posicao_atual.get("pending_live_protection_action") or "stop_refresh"),
                reason=str(posicao_atual.get("pending_live_protection_reason") or "pending_retry"),
                error=retry_error,
                target_position=target_position,
                partial_quantity=float(posicao_atual.get("pending_live_partial_quantity") or 0.0),
            )

    if _resolve_position_execution_profile(posicao_atual) == "managed":
        position_before_intrabar = dict(posicao_atual)
        management = evaluate_open_position(
            position_before_intrabar,
            current_price=price_now,
            timestamp=timestamp_atual,
            exit_at_stop_price=False,
        )
        if management.get("action") == "close":
            log_info(
                "Protecao intrabar acionou fechamento | motivo=stop_or_trailing | "
                f"preco={price_now:.2f}"
            )
            return _execute_ai_forced_exit(
                position_before_close=position_before_intrabar,
                candle_row=candle_row,
                risk_state=risk_state,
                timestamp_atual=timestamp_atual,
                runtime_snapshot=runtime_snapshot,
                runtime_session=runtime_session,
                live_execution_service=live_execution_service,
                live_execution_context=live_execution_context,
                close_reason="intrabar_stop_or_trailing",
                unified_decision_engine=unified_decision_engine,
            )

        if management.get("action") == "partial":
            managed_position = dict(management.get("position") or {})
            previous_quantity = float(position_before_intrabar.get("quantity", 0.0) or 0.0)
            partial_target_quantity = previous_quantity * 0.5
            if _live_execution_enabled():
                try:
                    partial_result = live_execution_service.submit_market_order(
                        context=live_execution_context,
                        symbol=config.SYMBOL,
                        timeframe=config.TIMEFRAME,
                        strategy_version=runtime_snapshot.get("strategy_version"),
                        signal_side=_opposite_signal_for_position(position_before_intrabar),
                        quantity=partial_target_quantity,
                        reduce_only=True,
                        source="bot_runner_intrabar_partial_exit",
                        testnet=bool(config.TESTNET),
                        leverage=int(getattr(config, "LEVERAGE", 1) or 1),
                        metadata={
                            "reason": "intrabar_partial_target_hit",
                            "entry_price": position_before_intrabar.get("entry_price"),
                            "partial_target": position_before_intrabar.get("partial_target"),
                            "price_now": price_now,
                        },
                    )
                    filled_quantity = min(
                        float(partial_result.get("quantity") or partial_target_quantity or 0.0),
                        partial_target_quantity,
                        previous_quantity,
                    )
                    managed_position["quantity"] = max(previous_quantity - filled_quantity, 0.0)
                    managed_position["realized_partial_pct"] = (
                        float(position_before_intrabar.get("realized_partial_pct", 0.0) or 0.0)
                        + _calculate_trade_pct_from_position(
                            position_before_intrabar,
                            float(partial_result.get("price") or position_before_intrabar.get("partial_target") or price_now),
                        )
                        * 0.5
                    )
                    managed_position["live_partial_realized_pct_accounted"] = float(
                        managed_position.get("realized_partial_pct", 0.0) or 0.0
                    )
                    managed_position = _refresh_live_managed_protective_stop(
                        execution_service=live_execution_service,
                        context=live_execution_context,
                        position_before=position_before_intrabar,
                        position_after=managed_position,
                    )
                    log_info(
                        f"Parcial intrabar executada; stop protegido | qty_restante={float(managed_position.get('quantity') or 0.0):.6f}"
                    )
                    return managed_position
                except Exception as partial_error:
                    log_info(f"Aviso: protecao intrabar falhou; estado anterior mantido: {partial_error}")
                    return _mark_pending_live_protection(
                        position_before_intrabar,
                        action="partial",
                        reason="intrabar_partial_target_hit",
                        error=partial_error,
                        target_position=managed_position,
                        partial_quantity=partial_target_quantity,
                    )

        managed_position = management.get("position")
        if managed_position is not None:
            previous_stop = float(position_before_intrabar.get("current_stop", 0.0) or 0.0)
            new_stop = float(managed_position.get("current_stop", 0.0) or 0.0)
            if abs(previous_stop - new_stop) > 1e-9:
                if _live_execution_enabled():
                    try:
                        managed_position = _refresh_live_managed_protective_stop(
                            execution_service=live_execution_service,
                            context=live_execution_context,
                            position_before=position_before_intrabar,
                            position_after=managed_position,
                        )
                        log_info(f"Trailing intrabar sincronizado | SL: {new_stop:.2f}")
                        return managed_position
                    except Exception as stop_error:
                        log_info(f"Aviso: trailing intrabar nao sincronizado; estado anterior mantido: {stop_error}")
                        return _mark_pending_live_protection(
                            position_before_intrabar,
                            action="stop_refresh",
                            reason="intrabar_trailing_stop_refresh",
                            error=stop_error,
                            target_position=managed_position,
                        )
                return managed_position

    if unified_decision_engine is None:
        return posicao_atual

    ai_exit = unified_decision_engine.should_exit_position(
        position=posicao_atual,
        candle_slice=candle_slice,
    )
    runtime_session["last_ai_position_monitor"] = {
        "monitored_at_utc": datetime.now(UTC).isoformat(),
        "candle_timestamp": str(timestamp_atual),
        "monitor_reason": ai_exit.get("monitor_reason"),
        "structure": ai_exit.get("structure") or {},
        "ai_decision": ai_exit.get("ai_decision") or {},
        "learning_stats": ai_exit.get("learning_stats") or {},
    }
    if not ai_exit.get("exit"):
        return posicao_atual

    manage_with_trailing, alignment = _should_manage_aligned_position_with_trailing(
        posicao_atual,
        candle_slice,
        ai_exit,
    )
    if manage_with_trailing:
        runtime_session["last_ai_position_monitor"]["monitor_reason"] = "ai_exit_blocked_trailing_management"
        runtime_session["last_ai_position_monitor"]["alignment"] = alignment
        log_info(
            "Saida IA intrabar bloqueada: mercado ainda favorece a posicao; trailing segue gerenciando | "
            f"side={alignment.get('side')} | pnl={float(alignment.get('unrealized_pct') or 0.0):.4f}%"
        )
        return posicao_atual

    log_info(
        "Monitor intrabar IA acionou saida | motivo:",
        str(ai_exit.get("reason") or "ai_intrabar_exit"),
        "| candle:",
        str(timestamp_atual),
    )
    return _execute_ai_forced_exit(
        position_before_close=posicao_atual,
        candle_row=candle_row,
        risk_state=risk_state,
        timestamp_atual=timestamp_atual,
        runtime_snapshot=runtime_snapshot,
        runtime_session=runtime_session,
        live_execution_service=live_execution_service,
        live_execution_context=live_execution_context,
        close_reason=str(ai_exit.get("reason") or "ai_intrabar_exit"),
        unified_decision_engine=unified_decision_engine,
    )


def _process_closed_candle(
    *,
    df,
    candle_index: int,
    params,
    posicao_atual,
    risk_state: dict,
    runtime_snapshot: dict,
    live_execution_service,
    risk_management_service,
    live_execution_context,
    runtime_session: dict,
    unified_decision_engine: UnifiedDecisionEngine | None = None,
):
    position_at_candle_start = posicao_atual
    candle_slice = df.iloc[: candle_index + 1]
    candle_row = candle_slice.iloc[-1]
    timestamp_atual = candle_row["timestamp"]
    preco = float(candle_row["close"])
    runtime_session["processed_candles"] = int(runtime_session.get("processed_candles", 0) or 0) + 1

    _roll_daily_state(risk_state, timestamp_atual)
    log_info(f"Novo candle detectado | preco: {preco:.2f}")

    if posicao_atual is not None:
        position_before_management = posicao_atual
        execution_profile = _resolve_position_execution_profile(position_before_management)
        previous_realized_partial_pct = float(position_before_management.get("realized_partial_pct", 0.0) or 0.0)
        gestao = (
            evaluate_native_bracket_position_on_candle(position_before_management, candle_row)
            if execution_profile == "native_bracket"
            else evaluate_managed_position_on_candle(
                position_before_management,
                candle_row,
                realized_partial_pct=previous_realized_partial_pct,
            )
        )
        live_positions = None
        if _live_execution_enabled():
            try:
                live_execution_service.reconcile_account_state(
                    context=live_execution_context,
                    symbol=config.SYMBOL,
                    timeframe=config.TIMEFRAME,
                    strategy_version=runtime_snapshot.get("strategy_version"),
                    testnet=bool(config.TESTNET),
                    source="bot_runner_candle",
                )
            except Exception as reconciliation_error:
                log_info(f"Aviso: falha na reconciliacao live do candle: {reconciliation_error}")
            try:
                live_positions = _find_live_positions(live_execution_context)
            except Exception as live_positions_error:
                log_info(f"Aviso: falha ao consultar posicoes live: {live_positions_error}")
                live_positions = None
        if execution_profile == "native_bracket":
            if _live_execution_enabled():
                open_positions = live_positions if live_positions is not None else _find_live_positions(live_execution_context)
                if open_positions:
                    if gestao["action"] == "hold":
                        posicao_atual = gestao["position"]
                    else:
                        posicao_atual = position_before_management
                        log_info(
                            "Aviso: candle sugere encerramento por bracket, "
                            "mas a exchange ainda reporta posicao aberta. "
                            "Mantendo a exchange como fonte de verdade."
                        )
                else:
                    trade_reason = (
                        str((gestao.get("closed_position") or {}).get("reason") or "exchange_reconciled")
                        if gestao["action"] == "close"
                        else "exchange_reconciled"
                    )
                    trade = _build_exchange_reconciled_close(
                        position_before_management,
                        candle_row,
                        live_execution_context,
                        reason=trade_reason,
                    )
                    posicao_atual = None
                    log_info(
                        "Saida live conciliada:",
                        trade["reason"],
                        "| resultado %:",
                        round(trade["gross_pct"], 4),
                    )
                    _update_risk_circuit_breaker(risk_state, trade, timestamp_atual)
                    _save_live_realization_event(
                        context=live_execution_context,
                        position=position_before_management,
                        trade=trade,
                        gross_pct_contribution=float(trade.get("gross_pct", 0.0) or 0.0),
                        event_type="live_trade_reconciled_closed",
                        event_status="reconciled",
                        message="Saida live reconciliada com fill da exchange.",
                        source="bot_runner_reconciliation",
                    )
                    _register_learning_trade(unified_decision_engine, trade)
        elif gestao["action"] == "close":
            trade = gestao["closed_position"]
            close_result = _close_runtime_paper_trade(position_before_management, trade)
            posicao_atual = None
            if close_result is not None:
                log_info(
                    f"Saida: {trade['reason']} | resultado %: {float(close_result['result_pct']):.4f} | "
                    f"pnl_usdt: {float(close_result['result_usdt']):.2f}"
                )
            else:
                log_info(f"Saida: {trade['reason']} | resultado %: {round(trade['gross_pct'], 4)}")
            _update_risk_circuit_breaker(risk_state, trade, timestamp_atual)
            _register_learning_trade(unified_decision_engine, trade)
        else:
            posicao_atual = gestao["position"]
            _update_runtime_paper_trade(posicao_atual)
        if execution_profile != "native_bracket" and gestao["action"] == "close":
            realized_partial_pct = float(gestao.get("realized_partial_pct", previous_realized_partial_pct) or 0.0)
            accounted_partial_pct = float(
                position_before_management.get("live_partial_realized_pct_accounted", 0.0) or 0.0
            )
            trade = _finalize_runtime_managed_trade(
                position_before_management,
                gestao["closed_position"],
                realized_partial_pct,
            )
            gross_pct_contribution = float(trade.get("gross_pct", 0.0) or 0.0) - accounted_partial_pct
            if _live_execution_enabled() and live_positions == []:
                trade = _finalize_runtime_managed_trade(
                    position_before_management,
                    _build_exchange_reconciled_close(
                        position_before_management,
                        candle_row,
                        live_execution_context,
                        reason=str(trade.get("reason") or "exchange_reconciled"),
                    ),
                    realized_partial_pct,
                )
                gross_pct_contribution = float(trade.get("gross_pct", 0.0) or 0.0) - accounted_partial_pct
                posicao_atual = None
                log_info(
                    "Saida live conciliada:",
                    trade["reason"],
                    "| resultado %:",
                    round(trade["gross_pct"], 4),
                )
                _update_risk_circuit_breaker(
                    risk_state,
                    {"gross_pct": gross_pct_contribution, "reason": trade.get("reason")},
                    timestamp_atual,
                )
                _save_live_realization_event(
                    context=live_execution_context,
                    position=position_before_management,
                    trade=trade,
                    gross_pct_contribution=gross_pct_contribution,
                    event_type="live_trade_reconciled_closed",
                    event_status="reconciled",
                    message="Saida managed reconciliada com a exchange.",
                    source="bot_runner_reconciliation",
                )
                _register_learning_trade(unified_decision_engine, trade)
            elif _live_execution_enabled():
                try:
                    exit_quantity = float(position_before_management.get("quantity", 0.0) or 0.0)
                    if exit_quantity <= 0:
                        raise RuntimeError("Posicao live sem quantity valida para fechamento.")
                    exit_result = live_execution_service.submit_market_order(
                        context=live_execution_context,
                        symbol=config.SYMBOL,
                        timeframe=config.TIMEFRAME,
                        strategy_version=runtime_snapshot.get("strategy_version"),
                        signal_side=_opposite_signal_for_position(position_before_management),
                        quantity=exit_quantity,
                        reduce_only=True,
                        source="bot_runner_exit",
                        testnet=bool(config.TESTNET),
                        leverage=int(getattr(config, "LEVERAGE", 1) or 1),
                        metadata={
                            "reason": trade.get("reason"),
                            "entry_price": position_before_management.get("entry_price"),
                            "best_price": position_before_management.get("best_price"),
                        },
                    )
                    exit_fill_price = float(exit_result.get("price") or 0.0)
                    if exit_fill_price > 0:
                        adjusted_closed_trade = dict(gestao["closed_position"])
                        adjusted_closed_trade["exit_price"] = exit_fill_price
                        adjusted_closed_trade["gross_pct"] = _calculate_trade_pct_from_position(
                            position_before_management,
                            exit_fill_price,
                        )
                        trade = _finalize_runtime_managed_trade(
                            position_before_management,
                            adjusted_closed_trade,
                            realized_partial_pct,
                        )
                        gross_pct_contribution = float(trade.get("gross_pct", 0.0) or 0.0) - accounted_partial_pct
                    posicao_atual = None
                    log_info(
                        "Saida live:",
                        trade["reason"],
                        "| resultado %:",
                        round(trade["gross_pct"], 4),
                        "| order:",
                        exit_result.get("exchange_order_id"),
                    )
                    # Cancelar ordens nativas pendentes (SL/TP) para evitar execuções órfãs
                    try:
                        live_execution_service.cancel_all_symbol_orders(
                            context=live_execution_context,
                            symbol=config.SYMBOL,
                        )
                        log_info("Ordens nativas pendentes canceladas.")
                    except Exception as cancel_exc:
                        log_info(f"Aviso: falha ao limpar ordens nativas no fechamento: {cancel_exc}")
                    _update_risk_circuit_breaker(
                        risk_state,
                        {"gross_pct": gross_pct_contribution, "reason": trade.get("reason")},
                        timestamp_atual,
                    )
                    _save_live_realization_event(
                        context=live_execution_context,
                        position=position_before_management,
                        trade=trade,
                        gross_pct_contribution=gross_pct_contribution,
                        event_type="live_trade_closed",
                        event_status="ok",
                        message="Saida live reduce-only executada.",
                        source="bot_runner_exit",
                    )
                    _register_learning_trade(unified_decision_engine, trade)
                except Exception as live_exit_error:
                    posicao_atual = position_before_management
                    log_info(f"Falha ao enviar saida live: {live_exit_error}")
            else:
                close_result = _close_runtime_paper_trade(position_before_management, trade)
                posicao_atual = None
                if close_result is not None:
                    log_info(
                        f"Saida: {trade['reason']} | resultado %: {float(close_result['result_pct']):.4f} | "
                        f"pnl_usdt: {float(close_result['result_usdt']):.2f}"
                    )
                else:
                    log_info(f"Saida: {trade['reason']} | resultado %: {round(trade['gross_pct'], 4)}")
                _update_risk_circuit_breaker(risk_state, trade, timestamp_atual)
                _register_learning_trade(unified_decision_engine, trade)
        elif execution_profile != "native_bracket" and _live_execution_enabled() and live_positions == []:
            trade = _finalize_runtime_managed_trade(
                position_before_management,
                _build_exchange_reconciled_close(position_before_management, candle_row, live_execution_context),
                previous_realized_partial_pct,
            )
            gross_pct_contribution = float(trade.get("gross_pct", 0.0) or 0.0) - float(
                position_before_management.get("live_partial_realized_pct_accounted", 0.0) or 0.0
            )
            posicao_atual = None
            log_info(
                "Saida live conciliada:",
                trade["reason"],
                "| resultado %:",
                round(trade["gross_pct"], 4),
            )
            _update_risk_circuit_breaker(
                risk_state,
                {"gross_pct": gross_pct_contribution, "reason": trade.get("reason")},
                timestamp_atual,
            )
            _save_live_realization_event(
                context=live_execution_context,
                position=position_before_management,
                trade=trade,
                gross_pct_contribution=gross_pct_contribution,
                event_type="live_trade_reconciled_closed",
                event_status="reconciled",
                message="Saida managed reconciliada sem posicao aberta na exchange.",
                source="bot_runner_reconciliation",
            )
            _register_learning_trade(unified_decision_engine, trade)
        elif execution_profile != "native_bracket":
            posicao_atual = gestao["position"]
            posicao_atual["realized_partial_pct"] = float(
                gestao.get("realized_partial_pct", previous_realized_partial_pct) or 0.0
            )
            posicao_atual["live_partial_realized_pct_accounted"] = float(
                position_before_management.get("live_partial_realized_pct_accounted", 0.0) or 0.0
            )
            posicao_atual["quantity"] = float(
                position_before_management.get("quantity")
                or posicao_atual.get("quantity")
                or 0.0
            )
            partial_just_triggered = (not bool(position_before_management.get("partial_taken", False))) and bool(
                posicao_atual.get("partial_taken", False)
            )
            if _live_execution_enabled() and partial_just_triggered:
                previous_quantity = float(position_before_management.get("quantity", 0.0) or 0.0)
                previous_accounted_partial_pct = float(
                    position_before_management.get("live_partial_realized_pct_accounted", 0.0) or 0.0
                )
                partial_target_quantity = previous_quantity * 0.5
                try:
                    if partial_target_quantity <= 0:
                        raise RuntimeError("Quantidade invalida para parcial live.")
                    try:
                        partial_trading_rules = live_execution_service.fetch_symbol_trading_rules(
                            live_execution_context,
                            symbol=config.SYMBOL,
                            testnet=bool(config.TESTNET),
                        )
                    except Exception:
                        partial_trading_rules = None
                    partial_reference_price = float(
                        position_before_management.get("partial_target")
                        or position_before_management.get("entry_price")
                        or 0.0
                    )
                    partial_operable, partial_operability = _is_reduce_only_quantity_operable(
                        quantity=partial_target_quantity,
                        reference_price=partial_reference_price,
                        trading_rules=partial_trading_rules,
                    )
                    if not partial_operable:
                        posicao_atual["quantity"] = previous_quantity
                        posicao_atual["realized_partial_pct"] = previous_realized_partial_pct
                        posicao_atual["live_partial_realized_pct_accounted"] = previous_accounted_partial_pct
                        posicao_atual["live_partial_skipped_reason"] = partial_operability.get("reason")
                        log_info(
                            "Parcial live ignorada por minimo da exchange | "
                            f"motivo={partial_operability.get('reason')} | "
                            f"qty={partial_operability.get('rounded_quantity')} | "
                            f"notional={partial_operability.get('rounded_notional')}"
                        )
                    else:
                        partial_result = live_execution_service.submit_market_order(
                            context=live_execution_context,
                            symbol=config.SYMBOL,
                            timeframe=config.TIMEFRAME,
                            strategy_version=runtime_snapshot.get("strategy_version"),
                            signal_side=_opposite_signal_for_position(position_before_management),
                            quantity=partial_target_quantity,
                            reduce_only=True,
                            source="bot_runner_partial_exit",
                            testnet=bool(config.TESTNET),
                            leverage=int(getattr(config, "LEVERAGE", 1) or 1),
                            metadata={
                                "reason": "partial_target_hit",
                                "entry_price": position_before_management.get("entry_price"),
                                "partial_target": position_before_management.get("partial_target"),
                            },
                        )
                        filled_quantity = min(
                            float(partial_result.get("quantity") or partial_target_quantity or 0.0),
                            partial_target_quantity,
                            previous_quantity,
                        )
                        realized_partial_delta = max(
                            float(posicao_atual.get("realized_partial_pct", 0.0) or 0.0) - previous_realized_partial_pct,
                            0.0,
                        )
                        if previous_quantity > 0 and partial_target_quantity > 0 and filled_quantity < partial_target_quantity:
                            realized_partial_delta *= min((filled_quantity / previous_quantity) / 0.5, 1.0)
                        posicao_atual["quantity"] = max(previous_quantity - filled_quantity, 0.0)
                        posicao_atual["realized_partial_pct"] = previous_realized_partial_pct + realized_partial_delta
                        posicao_atual["live_partial_realized_pct_accounted"] = (
                            previous_accounted_partial_pct + realized_partial_delta
                        )
                        partial_trade = {
                            "exit_price": float(partial_result.get("price") or position_before_management.get("partial_target") or 0.0),
                            "gross_pct": realized_partial_delta,
                            "reason": "partial_target_hit",
                        }
                        _update_risk_circuit_breaker(
                            risk_state,
                            {"gross_pct": realized_partial_delta, "reason": "partial_target_hit"},
                            timestamp_atual,
                        )
                        _save_live_realization_event(
                            context=live_execution_context,
                            position=position_before_management,
                            trade=partial_trade,
                            gross_pct_contribution=realized_partial_delta,
                            event_type="live_trade_partial",
                            event_status="ok",
                            message="Parcial live executada com reduce-only.",
                            source="bot_runner_partial_exit",
                        )
                        log_info(
                            f"Parcial live executada | qty: {filled_quantity:.6f} | restante: {float(posicao_atual['quantity']):.6f}"
                        )
                except Exception as partial_exit_error:
                    posicao_atual["quantity"] = previous_quantity
                    posicao_atual["realized_partial_pct"] = previous_realized_partial_pct
                    posicao_atual["live_partial_realized_pct_accounted"] = previous_accounted_partial_pct
                    posicao_atual["partial_taken"] = False
                    log_info(f"Aviso: falha ao executar parcial live: {partial_exit_error}")
                    posicao_atual = _mark_pending_live_protection(
                        posicao_atual,
                        action="partial",
                        reason="closed_candle_partial_target_hit",
                        error=partial_exit_error,
                        target_position=gestao["position"],
                        partial_quantity=partial_target_quantity,
                    )
            if _live_execution_enabled():
                try:
                    posicao_atual = _refresh_live_managed_protective_stop(
                        execution_service=live_execution_service,
                        context=live_execution_context,
                        position_before=position_before_management,
                        position_after=posicao_atual,
                    )
                except Exception as stop_sync_error:
                    log_info(f"Aviso: falha ao sincronizar stop live gerenciado: {stop_sync_error}")
                    posicao_atual = _mark_pending_live_protection(
                        posicao_atual,
                        action="stop_refresh",
                        reason="closed_candle_stop_refresh",
                        error=stop_sync_error,
                        target_position=posicao_atual,
                    )
            _update_runtime_paper_trade(posicao_atual)
            if partial_just_triggered:
                log_info("Parcial atingida; break-even e trailing ativados.")

    ai_forced_exit_triggered = False
    ai_decision = None
    if unified_decision_engine is not None:
        try:
            if posicao_atual is not None:
                ai_exit = unified_decision_engine.should_exit_position(
                    position=posicao_atual,
                    candle_slice=candle_slice,
                )
                ai_decision = ai_exit.get("ai_decision")
                if ai_exit.get("exit"):
                    manage_with_trailing, alignment = _should_manage_aligned_position_with_trailing(
                        posicao_atual,
                        candle_slice,
                        ai_exit,
                    )
                    if manage_with_trailing:
                        log_info(
                            "Saida IA bloqueada: mercado ainda favorece a posicao; trailing segue gerenciando | "
                            f"side={alignment.get('side')} | pnl={float(alignment.get('unrealized_pct') or 0.0):.4f}%"
                        )
                    else:
                        forced_position = _execute_ai_forced_exit(
                            position_before_close=posicao_atual,
                            candle_row=candle_row,
                            risk_state=risk_state,
                            timestamp_atual=timestamp_atual,
                            runtime_snapshot=runtime_snapshot,
                            runtime_session=runtime_session,
                            live_execution_service=live_execution_service,
                            live_execution_context=live_execution_context,
                            close_reason=str(ai_exit.get("reason") or "ai_forced_exit"),
                            unified_decision_engine=unified_decision_engine,
                        )
                        posicao_atual = forced_position
                        ai_forced_exit_triggered = posicao_atual is None

            resultado = unified_decision_engine.decide_entry(candle_slice, params)
            ai_decision = resultado.get("ai_decision") or ai_decision
            if ai_decision:
                runtime_session["last_ai_decision"] = {
                    "scored_at_utc": datetime.now(UTC).isoformat(),
                    "candle_timestamp": str(timestamp_atual),
                    **_serialize_ai_decision(ai_decision),
                }
        except Exception as ai_error:
            log_info(f"Aviso: falha no motor hibrido IA: {ai_error}")
            resultado = generate_entry_signal(candle_slice, params)
    else:
        resultado = generate_entry_signal(candle_slice, params)

    if ai_forced_exit_triggered:
        resultado = {
            "signal": "hold",
            "reason": "ai_exit_same_candle_cooldown",
            "setup": resultado.get("setup"),
            "score": resultado.get("score"),
            "atr": resultado.get("atr"),
            "decision_source": "ai_exit_cooldown",
            "ai_decision": ai_decision or {},
        }
    if position_at_candle_start is not None and posicao_atual is None:
        cooldown_registered_for = (runtime_session.get("reentry_cooldown") or {}).get("closed_candle_timestamp")
        if str(cooldown_registered_for or "") != str(timestamp_atual):
            _register_runtime_reentry_cooldown(
                runtime_session,
                position_at_candle_start,
                timestamp_atual,
                reason="position_closed_on_candle",
            )
    log_info(
        f"Sinal: {resultado['signal']} | motivo: {resultado['reason']} | origem: {resultado.get('decision_source', 'engine')}"
    )

    if posicao_atual is None and resultado["signal"] in {"buy", "sell"}:
        entry_execution_profile = _resolve_runtime_execution_profile()
        runtime_session["actionable_signal_count"] = int(runtime_session.get("actionable_signal_count", 0) or 0) + 1
        if not runtime_session.get("first_actionable_signal_at_utc"):
            runtime_session["first_actionable_signal_at_utc"] = datetime.now(UTC).isoformat()
            runtime_session["first_actionable_candle_timestamp"] = str(timestamp_atual)
        can_enter, block_reason = _entry_allowed(risk_state)
        reentry_block_reason = _same_side_reentry_block_reason(runtime_session, str(resultado["signal"]), timestamp_atual)
        if reentry_block_reason:
            log_info(f"Entrada bloqueada: {reentry_block_reason}")
            runtime_session["last_blocked_entry"] = {
                "blocked_at_utc": datetime.now(UTC).isoformat(),
                "candle_timestamp": str(timestamp_atual),
                "signal": resultado.get("signal"),
                "setup_name": ((resultado.get("setup") or {}).get("setup")),
                "score": resultado.get("score"),
                "reason": reentry_block_reason,
                "stage": "reentry_cooldown",
            }
        elif not can_enter:
            log_info(f"Entrada bloqueada: {block_reason}")
            runtime_session["last_blocked_entry"] = {
                "blocked_at_utc": datetime.now(UTC).isoformat(),
                "candle_timestamp": str(timestamp_atual),
                "signal": resultado.get("signal"),
                "setup_name": ((resultado.get("setup") or {}).get("setup")),
                "score": resultado.get("score"),
                "reason": block_reason,
                "stage": "risk_gate",
            }
        elif _live_execution_enabled():
            live_plan = _build_live_entry_plan(
                execution_service=live_execution_service,
                risk_management_service=risk_management_service,
                context=live_execution_context,
                signal_side=resultado["signal"],
                entry_price=preco,
                atr=float(resultado.get("atr", 0.0) or 0.0),
                execution_profile=entry_execution_profile,
                signal_result=resultado,
                timestamp=timestamp_atual,
            )
            if not live_plan.get("allowed", False):
                log_info(f"Entrada live bloqueada: {live_plan.get('reason')}")
                runtime_session["last_blocked_entry"] = {
                    "blocked_at_utc": datetime.now(UTC).isoformat(),
                    "candle_timestamp": str(timestamp_atual),
                    "signal": resultado.get("signal"),
                    "setup_name": ((resultado.get("setup") or {}).get("setup")),
                    "score": resultado.get("score"),
                    "reason": live_plan.get("reason"),
                    "stage": "live_plan",
                }
            else:
                execution_result = live_execution_service.submit_market_order(
                    context=live_execution_context,
                    symbol=config.SYMBOL,
                    timeframe=config.TIMEFRAME,
                    strategy_version=runtime_snapshot.get("strategy_version"),
                    signal_side=resultado["signal"],
                    quantity=float(live_plan.get("quantity", 0.0) or 0.0),
                    reduce_only=False,
                    source="bot_runner_entry",
                    testnet=bool(config.TESTNET),
                    leverage=int(getattr(config, "LEVERAGE", 1) or 1),
                    metadata={
                        "account_balance": live_plan.get("account_balance"),
                        "risk_amount": live_plan.get("risk_amount"),
                        "position_notional": live_plan.get("position_notional"),
                        "stop_loss_price": live_plan.get("stop_loss_price"),
                        "take_profit_price": live_plan.get("take_profit_price"),
                    },
                )
                preco_execucao = float(execution_result.get("price") or preco)
                position_execution_profile = _resolve_runtime_execution_profile(
                    str(live_plan.get("execution_profile") or entry_execution_profile)
                )
                posicao_atual = _build_runtime_position(
                    signal=resultado["signal"],
                    entry_price=preco_execucao,
                    timestamp=timestamp_atual,
                    atr=float(resultado.get("atr", 0.0) or 0.0),
                    execution_profile=position_execution_profile,
                    signal_result=resultado,
                )
                posicao_atual.update(
                    {
                        "quantity": float(
                            execution_result.get("quantity") or live_plan.get("quantity") or 0.0
                        ),
                        "account_reference_balance": float(live_plan.get("account_balance", 0.0) or 0.0),
                        "planned_position_notional": float(live_plan.get("position_notional", 0.0) or 0.0),
                        "risk_amount": float(live_plan.get("risk_amount", 0.0) or 0.0),
                        "execution_mode": "live",
                        "execution_profile": position_execution_profile,
                        "strategy_version": runtime_snapshot.get("strategy_version"),
                        "client_order_id": execution_result.get("client_order_id"),
                        "exchange_order_id": execution_result.get("exchange_order_id"),
                        "exchange_position_side": _signal_to_position_side(resultado["signal"]),
                        "entry_fill_price_source": execution_result.get("fill_price_source"),
                        "live_partial_realized_pct_accounted": 0.0,
                    }
                )

                try:
                    stop_result = live_execution_service.submit_stop_market_order(
                        context=live_execution_context,
                        symbol=config.SYMBOL,
                        side=_opposite_signal_for_position(posicao_atual),
                        stop_price=float(posicao_atual["current_stop"]),
                        quantity=float(posicao_atual["quantity"]),
                        testnet=bool(config.TESTNET),
                        metadata={
                            "reason": "initial_protective_stop",
                            "entry_order_id": execution_result.get("exchange_order_id"),
                        },
                    )
                    posicao_atual["protective_stop_order_id"] = stop_result.get("exchange_order_id")
                    posicao_atual["protective_stop_client_order_id"] = stop_result.get("client_order_id")
                    posicao_atual["protective_stop_price"] = float(stop_result.get("stop_price") or posicao_atual["current_stop"])
                    posicao_atual["last_stop_sync_at"] = datetime.now(UTC).isoformat()
                    if position_execution_profile == "native_bracket":
                        live_execution_service.submit_take_profit_market_order(
                            context=live_execution_context,
                            symbol=config.SYMBOL,
                            side=_opposite_signal_for_position(posicao_atual),
                            stop_price=float(posicao_atual["partial_target"]),
                            quantity=float(posicao_atual["quantity"]),
                            testnet=bool(config.TESTNET),
                            metadata={
                                "reason": "initial_take_profit",
                                "entry_order_id": execution_result.get("exchange_order_id"),
                            },
                        )
                        log_info(
                            f"Ordens nativas enviadas | SL: {float(posicao_atual['current_stop']):.2f} | "
                            f"TP: {float(posicao_atual['partial_target']):.2f}"
                        )
                    else:
                        log_info(
                            f"Stop catastrófico nativo enviado | SL: {float(posicao_atual['current_stop']):.2f} | "
                            "gestao: parcial/break-even/trailing"
                        )
                except Exception as sl_tp_exc:
                    log_info(f"Aviso: erro ao enviar ordens nativas SL/TP: {sl_tp_exc}")

                log_info(
                    "Entrada live:",
                    posicao_atual["side"],
                    "| preco:",
                    round(preco_execucao, 2),
                    "| qty:",
                    round(float(posicao_atual.get("quantity", 0.0) or 0.0), 6),
                )
                runtime_session["entry_count"] = int(runtime_session.get("entry_count", 0) or 0) + 1
                if not runtime_session.get("first_entry_at_utc"):
                    runtime_session["first_entry_at_utc"] = datetime.now(UTC).isoformat()
                    runtime_session["first_entry_candle_timestamp"] = str(timestamp_atual)
                    runtime_session["first_entry_delay_sec"] = round(
                        max(time.time() - float(runtime_session.get("started_at_epoch") or time.time()), 0.0),
                        2,
                    )
                runtime_session["last_entry"] = {
                    "entered_at_utc": datetime.now(UTC).isoformat(),
                    "candle_timestamp": str(timestamp_atual),
                    "side": posicao_atual.get("side"),
                    "setup_name": ((resultado.get("setup") or {}).get("setup")),
                    "score": resultado.get("score"),
                    "reason": resultado.get("reason"),
                    "execution_mode": posicao_atual.get("execution_mode"),
                    "execution_profile": posicao_atual.get("execution_profile"),
                    "entry_price": float(posicao_atual.get("entry_price") or 0.0),
                    "quantity": float(posicao_atual.get("quantity") or 0.0),
                }
        else:
            posicao_atual = _build_runtime_position(
                signal=resultado["signal"],
                entry_price=preco,
                timestamp=timestamp_atual,
                atr=float(resultado.get("atr", 0.0) or 0.0),
                execution_profile=entry_execution_profile,
                signal_result=resultado,
            )
            posicao_atual["execution_mode"] = "paper"
            posicao_atual["execution_profile"] = entry_execution_profile
            posicao_atual.update(_build_runtime_paper_position_metrics(posicao_atual, runtime_snapshot))
            paper_trade_id = _create_runtime_paper_trade(posicao_atual, resultado, runtime_snapshot)
            if paper_trade_id is not None:
                posicao_atual["paper_trade_id"] = int(paper_trade_id)
            log_info(
                f"Entrada: {posicao_atual['side']} | preco: {round(posicao_atual['entry_price'], 2)} | "
                f"qty: {float(posicao_atual.get('quantity', 0.0) or 0.0):.6f} | "
                f"notional_usdt: {float(posicao_atual.get('planned_position_notional', 0.0) or 0.0):.2f}"
            )
            runtime_session["entry_count"] = int(runtime_session.get("entry_count", 0) or 0) + 1
            if not runtime_session.get("first_entry_at_utc"):
                runtime_session["first_entry_at_utc"] = datetime.now(UTC).isoformat()
                runtime_session["first_entry_candle_timestamp"] = str(timestamp_atual)
                runtime_session["first_entry_delay_sec"] = round(
                    max(time.time() - float(runtime_session.get("started_at_epoch") or time.time()), 0.0),
                    2,
                )
            runtime_session["last_entry"] = {
                "entered_at_utc": datetime.now(UTC).isoformat(),
                "candle_timestamp": str(timestamp_atual),
                "side": posicao_atual.get("side"),
                "setup_name": ((resultado.get("setup") or {}).get("setup")),
                "score": resultado.get("score"),
                "reason": resultado.get("reason"),
                "execution_mode": posicao_atual.get("execution_mode"),
                "execution_profile": posicao_atual.get("execution_profile"),
                "entry_price": float(posicao_atual.get("entry_price") or 0.0),
                "quantity": float(posicao_atual.get("quantity") or 0.0),
            }
    elif resultado["signal"] in {"buy", "sell"}:
        setup_payload = resultado.get("setup") or {}
        signal_position_side = _signal_to_position_side(str(resultado.get("signal") or ""))
        open_position_side = None if posicao_atual is None else str(posicao_atual.get("side") or "").strip().lower()
        conflict_type = (
            "same_direction_position_open"
            if open_position_side == signal_position_side
            else "opposite_direction_position_open"
        )
        conflict_label = "mesma_direcao" if conflict_type == "same_direction_position_open" else "direcao_oposta"
        runtime_session["ignored_actionable_signal_count"] = int(
            runtime_session.get("ignored_actionable_signal_count", 0) or 0
        ) + 1
        runtime_session["last_ignored_actionable_signal"] = {
            "ignored_at_utc": datetime.now(UTC).isoformat(),
            "candle_timestamp": str(timestamp_atual),
            "signal": resultado.get("signal"),
            "signal_position_side": signal_position_side,
            "setup_name": (setup_payload.get("setup") if isinstance(setup_payload, dict) else None),
            "score": resultado.get("score"),
            "reason": resultado.get("reason"),
            "open_position_side": open_position_side,
            "open_position_entry_price": None if posicao_atual is None else float(posicao_atual.get("entry_price") or 0.0),
            "stage": "position_already_open",
            "conflict_type": conflict_type,
        }
        log_info(
            "Sinal acionavel ignorado: posicao ja aberta | "
            f"conflito={conflict_label} | "
            f"sinal={resultado.get('signal')} | "
            f"setup={(setup_payload.get('setup') if isinstance(setup_payload, dict) else 'n/a') or 'n/a'} | "
            f"posicao={open_position_side} | "
            f"entrada={None if posicao_atual is None else round(float(posicao_atual.get('entry_price') or 0.0), 2)} | "
            f"motivo={resultado.get('reason')}"
        )

    status_label = "position_open" if posicao_atual is not None else "signal_processed"
    _persist_runtime_state(
        snapshot=runtime_snapshot,
        status=status_label,
        timestamp_value=timestamp_atual,
        signal=resultado,
        position=posicao_atual,
        risk_state=risk_state,
        last_price=preco,
        runtime_session=runtime_session,
    )
    log_info("------")
    return timestamp_atual, posicao_atual, resultado


def main() -> None:
    _configure_runtime_logging()
    clear_runtime_stop_request(path=_runtime_stop_request_path())
    runtime_session = {
        "started_at_utc": datetime.now(UTC).isoformat(),
        "started_at_epoch": time.time(),
        "processed_candles": 0,
        "actionable_signal_count": 0,
        "entry_count": 0,
        "startup_entry_gate_pending": False,
        "first_actionable_signal_at_utc": None,
        "first_actionable_candle_timestamp": None,
        "first_entry_at_utc": None,
        "first_entry_candle_timestamp": None,
        "first_entry_delay_sec": None,
        "last_entry": None,
        "last_blocked_entry": None,
        "ignored_actionable_signal_count": 0,
        "last_ignored_actionable_signal": None,
        "reentry_cooldown": None,
    }
    symbol_override_report = config.apply_symbol_strategy_overrides(config.SYMBOL)
    applied_overrides = symbol_override_report.get("applied") or {}
    if applied_overrides:
        log_info(f"Overrides de simbolo aplicados | {config.SYMBOL} | {json.dumps(applied_overrides, ensure_ascii=True, sort_keys=True)}")
    write_runtime_process_state(
        pid=os.getpid(),
        use_testnet=bool(config.TESTNET),
        entrypoint=__file__,
        source=str(os.getenv("TRADER_BOT_LAUNCH_SOURCE", "bot_runner")).strip() or "bot_runner",
        command=" ".join(sys.argv),
        path=_runtime_process_state_path(),
        extra={
            "symbol": config.SYMBOL,
            "timeframe": config.TIMEFRAME,
            "runtime_key": _runtime_key(),
            "user_id": int(getattr(config, "SINGLE_USER_RUNTIME_USER_ID", 0) or 0),
            "account_id": str(getattr(config, "SINGLE_USER_RUNTIME_ACCOUNT_ID", "") or ""),
        },
    )
    _validate_real_mode_guards()
    _validate_runtime_symbol_approval()
    runtime_snapshot = _print_runtime_baseline_snapshot()

    params = StrategyParams()
    runtime_market_data_limit = _resolve_runtime_market_data_limit(params)
    ultimo_timestamp, posicao_atual, risk_state = _load_runtime_recovery_state(runtime_snapshot)
    posicao_atual = _attach_runtime_open_paper_trade(posicao_atual, runtime_snapshot)
    runtime_session["startup_entry_gate_pending"] = bool(
        _live_execution_enabled()
        and not bool(config.TESTNET)
        and bool(getattr(config, "BOT_WAIT_NEXT_CLOSED_CANDLE_ON_REAL_STARTUP", True))
        and posicao_atual is None
    )
    unified_decision_engine = UnifiedDecisionEngine(symbol=config.SYMBOL, timeframe=config.TIMEFRAME)
    ai_runtime_status = unified_decision_engine.ai_model.get_runtime_status()
    log_info(
        "Motor unificado | "
        f"ai_loaded={'yes' if ai_runtime_status.get('runtime_loaded') else 'no'} | "
        f"model={ai_runtime_status.get('runtime_version') or ai_runtime_status.get('model_version') or '-'} | "
        f"learning={'on' if bool(getattr(config.ProductionConfig, 'AI_ONLINE_LEARNING_ENABLED', True)) else 'off'}"
    )
    if ai_runtime_status.get("reason"):
        log_info(f"IA detalhe: {ai_runtime_status.get('reason')}")
    live_execution_service = LiveExecutionService(database=db) if _live_execution_enabled() else None
    risk_management_service = RiskManagementService(database=db) if _live_execution_enabled() else None
    live_execution_context = None
    user_data_stream = None

    bootstrap_df = _load_bootstrap_candles(runtime_market_data_limit)
    market_stream = StreamlinedTradingBot(
        symbol=config.SYMBOL,
        timeframe=config.TIMEFRAME,
        max_candles=runtime_market_data_limit,
        testnet=bool(config.TESTNET),
        allow_rest_fallback=bool(getattr(config, "BOT_ALLOW_REST_FALLBACK", False)),
        bootstrap_df=bootstrap_df,
    )
    stream_status = market_stream.get_current_status()
    log_info(
        "Feed de mercado ativo | "
        f"provider={stream_status.get('provider')} | "
        f"rest_fallback={'on' if bool(getattr(config, 'BOT_ALLOW_REST_FALLBACK', False)) else 'off'}"
    )
    _validate_stream_runtime_ready(stream_status)
    if _live_execution_enabled():
        live_execution_context, posicao_atual, user_data_stream = _prepare_live_execution_runtime(
            snapshot=runtime_snapshot,
            execution_service=live_execution_service,
            recovered_position=posicao_atual,
        )
    _persist_runtime_state(
        snapshot=runtime_snapshot,
        status=(
            "live_ready"
            if _live_execution_enabled()
            else ("recovered_bootstrap" if ultimo_timestamp is not None or posicao_atual is not None else "bootstrapped")
        ),
        timestamp_value=ultimo_timestamp,
        position=posicao_atual,
        risk_state=risk_state,
        runtime_session=runtime_session,
    )

    try:
        while not runtime_stop_requested(path=_runtime_stop_request_path()):
            try:
                include_current_candle = bool(
                    posicao_atual is not None
                    and bool(getattr(config.ProductionConfig, "AI_POSITION_MONITOR_INCLUDE_CURRENT_CANDLE", True))
                )
                df = market_stream.get_market_data(
                    limit=runtime_market_data_limit,
                    timeout=float(getattr(config, "BOT_WEBSOCKET_TIMEOUT_SEC", 25.0) or 25.0),
                    include_current_candle=include_current_candle,
                )
                if df.empty:
                    log_info("Nenhum candle retornado, aguardando...")
                    _persist_runtime_state(
                        snapshot=runtime_snapshot,
                        status="waiting_market_data",
                        risk_state=risk_state,
                        runtime_session=runtime_session,
                    )
                    _sleep_with_stop(config.POLL_SECONDS)
                    continue

                persisted_df = df
                if "is_closed" in df.columns:
                    persisted_df = df[df["is_closed"].fillna(False)].copy()
                inserted_candles = _persist_backtest_websocket_frame(persisted_df)
                if inserted_candles > 0:
                    log_info(f"Historico websocket salvo localmente: +{inserted_candles} candles.")

                df = calculate_indicators(df, params)
                timestamp_atual = df["timestamp"].iloc[-1]
                preco = float(df["close"].iloc[-1])

                if runtime_session.get("startup_entry_gate_pending") and posicao_atual is None:
                    startup_df = df
                    if "is_closed" in startup_df.columns:
                        closed_startup_df = startup_df[startup_df["is_closed"].fillna(False)].copy()
                        if not closed_startup_df.empty:
                            startup_df = closed_startup_df
                    ultimo_timestamp = startup_df["timestamp"].iloc[-1]
                    _roll_daily_state(risk_state, ultimo_timestamp)
                    runtime_session["startup_entry_gate_pending"] = False
                    log_info(
                        "Startup real protegido: candle atual marcado como referencia; "
                        "aguardando proximo candle fechado antes de nova entrada."
                    )
                    _persist_runtime_state(
                        snapshot=runtime_snapshot,
                        status="waiting_new_candle",
                        timestamp_value=ultimo_timestamp,
                        position=posicao_atual,
                        risk_state=risk_state,
                        last_price=float(startup_df["close"].iloc[-1]),
                        runtime_session=runtime_session,
                    )
                    _sleep_with_stop(config.POLL_SECONDS)
                    continue

                if ultimo_timestamp is None:
                    ultimo_timestamp = timestamp_atual
                    _roll_daily_state(risk_state, timestamp_atual)
                    log_info("Bot iniciado, aguardando novo candle fechado...")
                    _persist_runtime_state(
                        snapshot=runtime_snapshot,
                        status="waiting_new_candle",
                        timestamp_value=timestamp_atual,
                        position=posicao_atual,
                        risk_state=risk_state,
                        last_price=preco,
                        runtime_session=runtime_session,
                    )
                else:
                    pending_indexes = _get_pending_candle_indexes(df, ultimo_timestamp)
                    if pending_indexes:
                        if len(pending_indexes) > 1:
                            log_info(f"Gap detectado | candles pendentes: {len(pending_indexes)} | retomando processamento sequencial...")
                        for candle_index in pending_indexes:
                            ultimo_timestamp, posicao_atual, _ = _process_closed_candle(
                                df=df,
                                candle_index=candle_index,
                                params=params,
                                posicao_atual=posicao_atual,
                                risk_state=risk_state,
                                runtime_snapshot=runtime_snapshot,
                                live_execution_service=live_execution_service,
                                risk_management_service=risk_management_service,
                                live_execution_context=live_execution_context,
                                runtime_session=runtime_session,
                                unified_decision_engine=unified_decision_engine,
                            )
                        if posicao_atual is not None and include_current_candle:
                            posicao_atual = _monitor_open_position_intrabar(
                                df=df,
                                posicao_atual=posicao_atual,
                                risk_state=risk_state,
                                runtime_snapshot=runtime_snapshot,
                                live_execution_service=live_execution_service,
                                live_execution_context=live_execution_context,
                                runtime_session=runtime_session,
                                unified_decision_engine=unified_decision_engine,
                            )
                    else:
                        if posicao_atual is not None and include_current_candle:
                            posicao_atual = _monitor_open_position_intrabar(
                                df=df,
                                posicao_atual=posicao_atual,
                                risk_state=risk_state,
                                runtime_snapshot=runtime_snapshot,
                                live_execution_service=live_execution_service,
                                live_execution_context=live_execution_context,
                                runtime_session=runtime_session,
                                unified_decision_engine=unified_decision_engine,
                            )
                        log_info("Sem candle novo. Aguardando...")
                        _persist_runtime_state(
                            snapshot=runtime_snapshot,
                            status=("monitoring_open_position" if posicao_atual is not None and include_current_candle else "idle_same_candle"),
                            timestamp_value=timestamp_atual,
                            position=posicao_atual,
                            risk_state=risk_state,
                            last_price=preco,
                            runtime_session=runtime_session,
                        )

                _sleep_with_stop(config.POLL_SECONDS)
            except Exception as e:
                error_message = str(e or "")
                if "Sem dados no websocket e fallback REST desativado" in error_message:
                    log_info("Aguardando aquecimento do feed websocket...")
                    _persist_runtime_state(
                        snapshot=runtime_snapshot,
                        status="waiting_websocket_buffer",
                        timestamp_value=ultimo_timestamp,
                        position=posicao_atual,
                        risk_state=risk_state,
                        last_error=error_message,
                        runtime_session=runtime_session,
                    )
                    _sleep_with_stop(min(max(int(config.POLL_SECONDS), 2), 10))
                    continue
                log_info(f"Erro: {e}")
                _persist_runtime_state(
                    snapshot=runtime_snapshot,
                    status="error",
                    timestamp_value=ultimo_timestamp,
                    position=posicao_atual,
                    risk_state=risk_state,
                    last_error=str(e),
                    runtime_session=runtime_session,
                )
                _sleep_with_stop(60)
        log_info("Parada graciosa solicitada pelo runtime controller.")
    finally:
        _persist_runtime_state(
            snapshot=runtime_snapshot,
            status="stopped",
            timestamp_value=ultimo_timestamp,
            position=posicao_atual,
            risk_state=risk_state,
            runtime_session=runtime_session,
        )
        if user_data_stream is not None:
            try:
                user_data_stream.stop()
            except Exception as stop_user_stream_error:
                log_info(f"Aviso: falha ao encerrar user data stream: {stop_user_stream_error}")
        market_stream.stop()
        clear_runtime_process_state(path=_runtime_process_state_path())
        clear_runtime_stop_request(path=_runtime_stop_request_path())


if __name__ == "__main__":
    main()
