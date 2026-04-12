from __future__ import annotations

import time
from datetime import UTC, datetime

import config
from market_data import fetch_historical_candles_from_csv
from position_manager import create_position, evaluate_open_position
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
    except FileNotFoundError as exc:
        if bool(getattr(config, "BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP", True)):
            raise RuntimeError(
                "Bootstrap do bot bloqueado: CSV local obrigatorio nao encontrado em data/history "
                f"para {config.SYMBOL} {config.TIMEFRAME}. "
                "Suba o arquivo historico correspondente ou desative BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP."
            ) from exc
        print("Aviso: CSV de bootstrap nao encontrado. Bot iniciara apenas com buffer do websocket.")
        return None


def main() -> None:
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
                    print(f"Novo candle detectado | preco: {preco:.2f}")

                    if posicao_atual is not None:
                        gestao = evaluate_open_position(posicao_atual, preco, timestamp_atual)
                        if gestao["action"] == "close":
                            trade = gestao["closed_position"]
                            posicao_atual = None
                            print("Saida:", trade["reason"], "| resultado %:", round(trade["gross_pct"], 4))
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
                            print("Entrada:", posicao_atual["side"], "| preco:", round(posicao_atual["entry_price"], 2))

                    print("------")
                else:
                    print("Sem candle novo. Aguardando...")

                time.sleep(config.POLL_SECONDS)
            except Exception as e:
                print("Erro:", e)
                time.sleep(60)
    finally:
        market_stream.stop()


if __name__ == "__main__":
    main()
