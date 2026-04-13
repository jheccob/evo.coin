from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import config
from database.database import db
from market_data import fetch_historical_candles_from_csv
from position_manager import create_position, evaluate_open_position
from services.live_execution_service import LiveExecutionService
from services.risk_management_service import RiskManagementService
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal
from trading_bot_websocket import StreamlinedTradingBot


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
    print(f"Inicializando bot | modo: {mode_label} | symbol: {config.SYMBOL} | timeframe: {config.TIMEFRAME}")

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

    print(
        "Guard-rails real OK | "
        f"risk_per_trade={risk_per_trade:.2f}% | "
        f"max_daily_loss={float(getattr(config, 'MAX_DAILY_REAL_LOSS_PCT', 2.5)):.2f}% | "
        f"max_consecutive_losses={int(getattr(config, 'MAX_CONSECUTIVE_REAL_LOSSES', 4))}"
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
    print(headline)
    print("Runtime snapshot:", json.dumps(snapshot, ensure_ascii=True, sort_keys=True))
    return snapshot


def _runtime_key() -> str:
    return f"primary:{config.SYMBOL}:{config.TIMEFRAME}"


def _live_execution_enabled() -> bool:
    return bool(config.ProductionConfig.ENABLE_LIVE_EXECUTION)


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


def _resolve_stop_loss_pct(signal: str) -> float:
    return float(config.LONG_STOP_LOSS_PCT if _signal_to_position_side(signal) == "long" else config.SHORT_STOP_LOSS_PCT)


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


def _build_live_entry_plan(
    *,
    execution_service: LiveExecutionService,
    risk_management_service: RiskManagementService,
    context: dict,
    signal_side: str,
    entry_price: float,
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
    account_balance = float(balance_snapshot.get("total") or 0.0)
    if account_balance <= 0:
        account_balance = float(balance_snapshot.get("free") or 0.0)
    if account_balance <= 0:
        return {
            "allowed": False,
            "reason": "Saldo indisponivel para calcular a ordem live.",
            "balance_snapshot": balance_snapshot,
        }

    sizing = risk_management_service.calculate_position_size(
        account_balance=account_balance,
        entry_price=float(entry_price),
        stop_loss_pct=_resolve_stop_loss_pct(signal_side),
        risk_pct=float(config.RISK_PER_TRADE_PCT),
    )
    quantity = float(sizing.get("quantity", 0.0) or 0.0)
    if quantity <= 0:
        return {
            "allowed": False,
            "reason": "Quantidade calculada zerada para a ordem live.",
            "balance_snapshot": balance_snapshot,
            "sizing": sizing,
        }

    return {
        "allowed": True,
        "reason": "",
        "account_balance": account_balance,
        "balance_snapshot": balance_snapshot,
        **sizing,
    }


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
        print("Aviso: estado local indicava posicao aberta, mas a exchange nao possui posicao. Limpando recovery local.")
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
    print(
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
        "partial_taken": position.get("partial_taken"),
        "break_even_active": position.get("break_even_active"),
        "atr": position.get("atr"),
        "quantity": position.get("quantity"),
        "execution_mode": position.get("execution_mode"),
        "client_order_id": position.get("client_order_id"),
        "exchange_order_id": position.get("exchange_order_id"),
        "exchange_position_side": position.get("exchange_position_side"),
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
        },
    }
    try:
        db.upsert_bot_runtime_state(payload)
    except Exception as exc:
        print("Aviso: falha ao persistir estado do runtime:", exc)


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
        "atr",
        "quantity",
    ):
        if restored.get(key) not in (None, ""):
            restored[key] = float(restored[key])
    restored["partial_taken"] = bool(restored.get("partial_taken", False))
    restored["break_even_active"] = bool(restored.get("break_even_active", False))
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
    print(
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
        print(f"Novo dia operacional: {day} | limites diarios resetados.")


def _update_risk_circuit_breaker(state: dict, trade: dict, timestamp_value) -> None:
    _roll_daily_state(state, timestamp_value)
    result_pct = float(trade.get("gross_pct", 0.0) or 0.0)
    state["daily_realized_pct"] += result_pct

    if result_pct < 0:
        state["consecutive_losses"] += 1
    else:
        state["consecutive_losses"] = 0

    print(
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
        print("Bloqueio de seguranca:", state["block_reason"])
        return

    if state["consecutive_losses"] >= max_consecutive_losses:
        state["blocked"] = True
        state["block_reason"] = (
            f"Circuit breaker por sequencia: {state['consecutive_losses']} perdas "
            f"(limite {max_consecutive_losses})."
        )
        print("Bloqueio de seguranca:", state["block_reason"])


def _entry_allowed(state: dict) -> tuple[bool, str]:
    if state.get("blocked"):
        return False, str(state.get("block_reason") or "Runtime bloqueado por seguranca.")
    return True, ""


def _load_bootstrap_candles():
    bootstrap_limit = max(
        int(getattr(config, "BOT_BOOTSTRAP_CANDLES", config.LIMIT) or config.LIMIT),
        max(int(config.LIMIT), 200),
    )
    try:
        bootstrap_df = fetch_historical_candles_from_csv(
            config.SYMBOL,
            config.TIMEFRAME,
            total_limit=bootstrap_limit,
        )
        print(f"Bootstrap local carregado: {len(bootstrap_df)} candles.")
        return bootstrap_df
    except FileNotFoundError:
        if bool(getattr(config, "BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP", False)):
            print(
                "Aviso: BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP estava ativo, mas o runtime atual nao exige mais CSV obrigatorio. "
                "O bot iniciara apenas com buffer do websocket."
            )
        else:
            print("Aviso: CSV de bootstrap nao encontrado. Bot iniciara apenas com buffer do websocket.")
        return None


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
    for idx, candle_timestamp in enumerate(df["timestamp"].tolist()):
        if candle_timestamp > ultimo_timestamp:
            pending_indexes.append(idx)
    return pending_indexes


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
):
    candle_slice = df.iloc[: candle_index + 1]
    timestamp_atual = candle_slice["timestamp"].iloc[-1]
    preco = float(candle_slice["close"].iloc[-1])

    _roll_daily_state(risk_state, timestamp_atual)
    print(f"Novo candle detectado | preco: {preco:.2f}")

    if posicao_atual is not None:
        position_before_management = posicao_atual
        gestao = evaluate_open_position(posicao_atual, preco, timestamp_atual)
        if gestao["action"] == "close":
            trade = gestao["closed_position"]
            if _live_execution_enabled():
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
                    posicao_atual = None
                    print(
                        "Saida live:",
                        trade["reason"],
                        "| resultado %:",
                        round(trade["gross_pct"], 4),
                        "| order:",
                        exit_result.get("exchange_order_id"),
                    )
                    _update_risk_circuit_breaker(risk_state, trade, timestamp_atual)
                except Exception as live_exit_error:
                    posicao_atual = position_before_management
                    print("Falha ao enviar saida live:", live_exit_error)
            else:
                posicao_atual = None
                print("Saida:", trade["reason"], "| resultado %:", round(trade["gross_pct"], 4))
                _update_risk_circuit_breaker(risk_state, trade, timestamp_atual)
        else:
            posicao_atual = gestao["position"]
            if gestao["action"] == "partial":
                print("Parcial atingida; break-even e trailing ativados.")

    resultado = generate_entry_signal(candle_slice, params)
    print("Sinal:", resultado["signal"], "| motivo:", resultado["reason"])

    if posicao_atual is None and resultado["signal"] in {"buy", "sell"}:
        can_enter, block_reason = _entry_allowed(risk_state)
        if not can_enter:
            print("Entrada bloqueada:", block_reason)
        elif _live_execution_enabled():
            live_plan = _build_live_entry_plan(
                execution_service=live_execution_service,
                risk_management_service=risk_management_service,
                context=live_execution_context,
                signal_side=resultado["signal"],
                entry_price=preco,
            )
            if not live_plan.get("allowed", False):
                print("Entrada live bloqueada:", live_plan.get("reason"))
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
                    },
                )
                posicao_atual = create_position(
                    resultado["signal"],
                    entry_price=preco,
                    timestamp=timestamp_atual,
                    atr=float(resultado["atr"]),
                )
                posicao_atual.update(
                    {
                        "quantity": float(
                            execution_result.get("quantity") or live_plan.get("quantity") or 0.0
                        ),
                        "execution_mode": "live",
                        "client_order_id": execution_result.get("client_order_id"),
                        "exchange_order_id": execution_result.get("exchange_order_id"),
                        "exchange_position_side": _signal_to_position_side(resultado["signal"]),
                    }
                )
                print(
                    "Entrada live:",
                    posicao_atual["side"],
                    "| preco:",
                    round(posicao_atual["entry_price"], 2),
                    "| qty:",
                    round(float(posicao_atual.get("quantity", 0.0) or 0.0), 6),
                )
        else:
            posicao_atual = create_position(
                resultado["signal"],
                entry_price=preco,
                timestamp=timestamp_atual,
                atr=float(resultado["atr"]),
            )
            print("Entrada:", posicao_atual["side"], "| preco:", round(posicao_atual["entry_price"], 2))

    status_label = "position_open" if posicao_atual is not None else "signal_processed"
    _persist_runtime_state(
        snapshot=runtime_snapshot,
        status=status_label,
        timestamp_value=timestamp_atual,
        signal=resultado,
        position=posicao_atual,
        risk_state=risk_state,
        last_price=preco,
    )
    print("------")
    return timestamp_atual, posicao_atual, resultado


def main() -> None:
    _validate_real_mode_guards()
    runtime_snapshot = _print_runtime_baseline_snapshot()

    params = StrategyParams()
    ultimo_timestamp, posicao_atual, risk_state = _load_runtime_recovery_state(runtime_snapshot)
    live_execution_service = LiveExecutionService(database=db) if _live_execution_enabled() else None
    risk_management_service = RiskManagementService(database=db) if _live_execution_enabled() else None
    live_execution_context = None
    user_data_stream = None

    bootstrap_df = _load_bootstrap_candles()
    market_stream = StreamlinedTradingBot(
        symbol=config.SYMBOL,
        timeframe=config.TIMEFRAME,
        max_candles=max(int(config.LIMIT), int(getattr(config, "BOT_BOOTSTRAP_CANDLES", config.LIMIT)), 300),
        testnet=bool(config.TESTNET),
        allow_rest_fallback=bool(getattr(config, "BOT_ALLOW_REST_FALLBACK", False)),
        bootstrap_df=bootstrap_df,
    )
    stream_status = market_stream.get_current_status()
    print(
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
    )

    try:
        while True:
            try:
                df = market_stream.get_market_data(
                    limit=max(int(config.LIMIT), 200),
                    timeout=float(getattr(config, "BOT_WEBSOCKET_TIMEOUT_SEC", 25.0) or 25.0),
                    include_current_candle=False,
                )
                if df.empty:
                    print("Nenhum candle retornado, aguardando...")
                    _persist_runtime_state(
                        snapshot=runtime_snapshot,
                        status="waiting_market_data",
                        risk_state=risk_state,
                    )
                    time.sleep(config.POLL_SECONDS)
                    continue

                df = calculate_indicators(df, params)
                timestamp_atual = df["timestamp"].iloc[-1]
                preco = float(df["close"].iloc[-1])

                if ultimo_timestamp is None:
                    ultimo_timestamp = timestamp_atual
                    _roll_daily_state(risk_state, timestamp_atual)
                    print("Bot iniciado, aguardando novo candle fechado...")
                    _persist_runtime_state(
                        snapshot=runtime_snapshot,
                        status="waiting_new_candle",
                        timestamp_value=timestamp_atual,
                        position=posicao_atual,
                        risk_state=risk_state,
                        last_price=preco,
                    )
                else:
                    pending_indexes = _get_pending_candle_indexes(df, ultimo_timestamp)
                    if pending_indexes:
                        if len(pending_indexes) > 1:
                            print(f"Gap detectado | candles pendentes: {len(pending_indexes)} | retomando processamento sequencial...")
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
                            )
                    else:
                        print("Sem candle novo. Aguardando...")
                        _persist_runtime_state(
                            snapshot=runtime_snapshot,
                            status="idle_same_candle",
                            timestamp_value=timestamp_atual,
                            position=posicao_atual,
                            risk_state=risk_state,
                            last_price=preco,
                        )

                time.sleep(config.POLL_SECONDS)
            except Exception as e:
                error_message = str(e or "")
                if "Sem dados no websocket e fallback REST desativado" in error_message:
                    print("Aguardando aquecimento do feed websocket...")
                    _persist_runtime_state(
                        snapshot=runtime_snapshot,
                        status="waiting_websocket_buffer",
                        timestamp_value=ultimo_timestamp,
                        position=posicao_atual,
                        risk_state=risk_state,
                        last_error=error_message,
                    )
                    time.sleep(min(max(int(config.POLL_SECONDS), 2), 10))
                    continue
                print("Erro:", e)
                _persist_runtime_state(
                    snapshot=runtime_snapshot,
                    status="error",
                    timestamp_value=ultimo_timestamp,
                    position=posicao_atual,
                    risk_state=risk_state,
                    last_error=str(e),
                )
                time.sleep(60)
    finally:
        _persist_runtime_state(
            snapshot=runtime_snapshot,
            status="stopped",
            timestamp_value=ultimo_timestamp,
            position=posicao_atual,
            risk_state=risk_state,
        )
        if user_data_stream is not None:
            try:
                user_data_stream.stop()
            except Exception as stop_user_stream_error:
                print("Aviso: falha ao encerrar user data stream:", stop_user_stream_error)
        market_stream.stop()


if __name__ == "__main__":
    main()
