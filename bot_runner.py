from __future__ import annotations

import time
from datetime import UTC, datetime

import config
from market_data import fetch_candles
from position_manager import create_position, evaluate_open_position
from strategy_engine import StrategyParams, calculate_indicators, generate_entry_signal


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


def _roll_daily_state(state: dict, timestamp_value) -> None:
    day = _resolve_day(timestamp_value)
    if state["day"] != day:
        state["day"] = day
        state["daily_realized_pct"] = 0.0
        state["blocked"] = False
        state["block_reason"] = ""
        print(f"Novo dia operacional: {day} | limites diários resetados.")


def _update_risk_circuit_breaker(state: dict, trade: dict, timestamp_value) -> None:
    _roll_daily_state(state, timestamp_value)
    result_pct = float(trade.get("gross_pct", 0.0) or 0.0)
    state["daily_realized_pct"] += result_pct

    if result_pct < 0:
        state["consecutive_losses"] += 1
    else:
        state["consecutive_losses"] = 0

    print(
        "Risco sessão | "
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
            f"Circuit breaker diário: perda {state['daily_realized_pct']:.4f}% "
            f"(limite -{abs(max_daily_loss):.2f}%)."
        )
        print("Bloqueio de segurança:", state["block_reason"])
        return

    if state["consecutive_losses"] >= max_consecutive_losses:
        state["blocked"] = True
        state["block_reason"] = (
            f"Circuit breaker por sequência: {state['consecutive_losses']} perdas "
            f"(limite {max_consecutive_losses})."
        )
        print("Bloqueio de segurança:", state["block_reason"])


def _entry_allowed(state: dict) -> tuple[bool, str]:
    if state.get("blocked"):
        return False, str(state.get("block_reason") or "Runtime bloqueado por segurança.")
    return True, ""


_validate_real_mode_guards()

ultimo_timestamp = None
posicao_atual = None
params = StrategyParams()
risk_state = {
    "day": None,
    "daily_realized_pct": 0.0,
    "consecutive_losses": 0,
    "blocked": False,
    "block_reason": "",
}


while True:
    try:
        df = fetch_candles(config.SYMBOL, config.TIMEFRAME, limit=config.LIMIT, testnet=config.TESTNET)
        if df.empty:
            print("Nenhum candle retornado, aguardando...")
            time.sleep(config.POLL_SECONDS)
            continue

        df = calculate_indicators(df, params)
        timestamp_atual = df["timestamp"].iloc[-1]

        if ultimo_timestamp is None:
            ultimo_timestamp = timestamp_atual
            _roll_daily_state(risk_state, timestamp_atual)
            print("Bot iniciado, aguardando novo candle fechado...")
        elif timestamp_atual != ultimo_timestamp:
            ultimo_timestamp = timestamp_atual
            _roll_daily_state(risk_state, timestamp_atual)
            preco = float(df["close"].iloc[-1])
            print(f"Novo candle detectado | preço: {preco:.2f}")

            if posicao_atual is not None:
                gestao = evaluate_open_position(posicao_atual, preco, timestamp_atual)
                if gestao["action"] == "close":
                    trade = gestao["closed_position"]
                    posicao_atual = None
                    print("Saída:", trade["reason"], "| resultado %:", round(trade["gross_pct"], 4))
                    _update_risk_circuit_breaker(risk_state, trade, timestamp_atual)
                else:
                    posicao_atual = gestao["position"]
                    if gestao["action"] == "partial":
                        print("Parcial atingida; break-even e trailing ativados.")

            resultado = generate_entry_signal(df, params)
            print("Sinal:", resultado["signal"], "| motivo:", resultado["reason"])

            if posicao_atual is None and resultado["signal"] in {"buy", "sell"}:
                can_enter, block_reason = _entry_allowed(risk_state)
                if not can_enter:
                    print("Entrada bloqueada:", block_reason)
                else:
                    posicao_atual = create_position(
                        resultado["signal"],
                        entry_price=preco,
                        timestamp=timestamp_atual,
                        atr=float(resultado["atr"]),
                    )
                    print("Entrada:", posicao_atual["side"], "| preço:", round(posicao_atual["entry_price"], 2))

            print("------")
        else:
            print("Sem candle novo. Aguardando...")

        time.sleep(config.POLL_SECONDS)
    except Exception as e:
        print("Erro:", e)
        time.sleep(60)
