import pandas as pd
import gzip
import os
import config


def _build_exchange_candidates(testnet: bool = False):
    import ccxt
    candidates = []

    base_config = {"enableRateLimit": True}
    exchange_cls = getattr(ccxt, "binanceusdm", None)
    if exchange_cls is not None:
        candidates.append(("binanceusdm", exchange_cls(dict(base_config))))

    # Fallback para Binance spot pública
    candidates.append(("binance", ccxt.binance(dict(base_config))))

    # Fallback adicional para regiões onde Binance pode estar bloqueada
    bybit_cls = getattr(ccxt, "bybit", None)
    if bybit_cls is not None:
        candidates.append(
            (
                "bybit",
                bybit_cls(
                    {
                        **base_config,
                        "options": {"defaultType": "swap"},
                    }
                ),
            )
        )

    if testnet:
        for _, exchange in candidates:
            try:
                exchange.set_sandbox_mode(True)
            except Exception:
                pass

    return candidates


def get_exchange(testnet: bool = False):
    return _build_exchange_candidates(testnet=testnet)[0][1]


def _is_restricted_location_error(exc: Exception) -> bool:
    text = str(exc or "").lower()
    return "restricted location" in text or "service unavailable from a restricted location" in text or " 451 " in f" {text} "


def _fetch_ohlcv_with_exchange_fallback(
    symbol: str,
    timeframe: str,
    limit: int,
    testnet: bool = False,
):
    last_error = None
    errors = []
    for exchange_name, exchange in _build_exchange_candidates(testnet=testnet):
        try:
            resolved_symbol = _resolve_exchange_symbol(exchange, symbol)
            ohlcv = exchange.fetch_ohlcv(resolved_symbol, timeframe=timeframe, limit=limit)
            if ohlcv:
                return exchange, resolved_symbol, ohlcv
        except Exception as exc:
            last_error = exc
            errors.append(f"{exchange_name}: {exc}")
            if not _is_restricted_location_error(exc):
                # Mantemos fallback mesmo para outros erros transitórios.
                continue

    if last_error is None:
        raise RuntimeError("Falha ao obter candles: nenhuma exchange disponível.")

    raise RuntimeError(
        "Falha ao obter candles em todas as exchanges candidatas. "
        + " | ".join(errors)
    ) from last_error


def _symbol_candidates(symbol: str):
    raw_symbol = str(symbol or "").strip()
    if not raw_symbol:
        return []

    candidates = [raw_symbol]
    cleaned = raw_symbol.upper().replace("-", "").replace(" ", "")

    if "/" in cleaned:
        base, quote_part = cleaned.split("/", 1)
        quote = quote_part.split(":", 1)[0]
        if ":" not in cleaned:
            candidates.append(f"{base}/{quote}:{quote}")
        else:
            candidates.append(f"{base}/{quote}")
        candidates.append(f"{base}{quote}")
    else:
        compact = cleaned.replace(":", "")
        candidates.append(compact)
        if compact.endswith("USDT") and len(compact) > 4:
            base = compact[:-4]
            quote = "USDT"
            candidates.append(f"{base}/{quote}")
            candidates.append(f"{base}/{quote}:{quote}")

    unique = []
    seen = set()
    for value in candidates:
        token = str(value or "").strip()
        if token and token not in seen:
            unique.append(token)
            seen.add(token)
    return unique


def _resolve_exchange_symbol(exchange, symbol: str) -> str:
    candidates = _symbol_candidates(symbol)
    if not candidates:
        return symbol

    try:
        markets = exchange.load_markets()
    except Exception:
        return symbol

    for candidate in candidates:
        if candidate in markets:
            return candidate

    by_lower = {key.lower(): key for key in markets.keys()}
    for candidate in candidates:
        matched = by_lower.get(candidate.lower())
        if matched:
            return matched

    markets_by_id = getattr(exchange, "markets_by_id", {}) or {}
    for candidate in candidates:
        lookup_id = candidate.replace("/", "").replace(":", "")
        rows = markets_by_id.get(lookup_id)
        if rows:
            if isinstance(rows, list) and rows:
                symbol_from_id = rows[0].get("symbol")
                if symbol_from_id:
                    return symbol_from_id
            elif isinstance(rows, dict):
                symbol_from_id = rows.get("symbol")
                if symbol_from_id:
                    return symbol_from_id

    return symbol


def _timeframe_to_milliseconds(timeframe: str) -> int:
    raw = str(timeframe or "15m").strip().lower()
    if not raw:
        return 15 * 60 * 1000
    unit = raw[-1]
    try:
        value = int(raw[:-1] or "1")
    except ValueError:
        return 15 * 60 * 1000
    if unit == "m":
        return value * 60 * 1000
    if unit == "h":
        return value * 60 * 60 * 1000
    if unit == "d":
        return value * 24 * 60 * 60 * 1000
    return 15 * 60 * 1000


def _apply_is_closed_flag(df: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    if df.empty or "timestamp" not in df.columns:
        df["is_closed"] = True
        return df

    timeframe_ms = _timeframe_to_milliseconds(timeframe)
    now_utc = pd.Timestamp.now(tz="UTC")
    close_times = df["timestamp"] + pd.to_timedelta(timeframe_ms, unit="ms")
    df["is_closed"] = (close_times <= now_utc).fillna(False)
    return df


def _candles_to_dataframe(ohlcv, timeframe: str):
    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    df = _apply_is_closed_flag(df, timeframe)
    return df


def fetch_candles(symbol: str, timeframe: str, limit: int = 500, testnet: bool = False):
    _, _, ohlcv = _fetch_ohlcv_with_exchange_fallback(
        symbol=symbol,
        timeframe=timeframe,
        limit=limit,
        testnet=testnet,
    )
    return _candles_to_dataframe(ohlcv, timeframe)


def fetch_historical_candles(symbol: str, timeframe: str, total_limit: int = 2000, batch_limit: int = 500, testnet: bool = False):
    exchange, resolved_symbol, _ = _fetch_ohlcv_with_exchange_fallback(
        symbol=symbol,
        timeframe=timeframe,
        limit=min(max(int(batch_limit or 1), 1), max(int(total_limit or 1), 1)),
        testnet=testnet,
    )
    timeframe_ms = exchange.parse_timeframe(timeframe) * 1000
    since = exchange.milliseconds() - (total_limit * timeframe_ms)
    all_ohlcv = []

    while len(all_ohlcv) < total_limit:
        current_limit = min(batch_limit, total_limit - len(all_ohlcv))
        batch = exchange.fetch_ohlcv(resolved_symbol, timeframe=timeframe, since=since, limit=current_limit)
        if not batch:
            break
        all_ohlcv.extend(batch)
        since = batch[-1][0] + timeframe_ms
        if len(batch) < current_limit:
            break

    unique = list({row[0]: row for row in all_ohlcv}.values())
    unique.sort(key=lambda row: row[0])
    return _candles_to_dataframe(unique[-total_limit:], timeframe)


def _normalize_symbol_for_csv(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").replace(":", "").upper()


def _normalize_timeframe_for_csv(timeframe: str) -> str:
    return timeframe.replace(" ", "").lower()


def resolve_history_csv_path(symbol: str, timeframe: str) -> str:
    """
    Resolve o arquivo histórico local aceitando tanto .csv.gz quanto .csv.
    Se nada existir, retorna o caminho preferencial .csv.gz para manter
    mensagens de erro e documentação consistentes.
    """
    symbol = _normalize_symbol_for_csv(symbol)
    timeframe = _normalize_timeframe_for_csv(timeframe)

    base_name = f"{symbol}_{timeframe}"
    candidates = [
        os.path.join(config.HISTORY_DATA_DIR, f"{base_name}.csv.gz"),
        os.path.join(config.HISTORY_DATA_DIR, f"{base_name}.csv"),
    ]

    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate

    return candidates[0]


def fetch_historical_candles_from_csv(symbol: str, timeframe: str, total_limit: int = 2000):
    """
    Lê dados históricos de um arquivo CSV local gzipado.
    Exemplo: BTCUSDT_15m.csv.gz localizado em HISTORY_DATA_DIR.
    """
    file_path = resolve_history_csv_path(symbol, timeframe)
    
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")
    
    try:
        if file_path.endswith(".gz"):
            df = pd.read_csv(gzip.open(file_path, 'rt'))
        else:
            df = pd.read_csv(file_path)
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        
        # Converter colunas numéricas
        for column in ['open', 'high', 'low', 'close', 'volume']:
            df[column] = pd.to_numeric(df[column], errors='coerce')

        if 'is_closed' not in df.columns:
            df['is_closed'] = True

        # Retornar os últimos N candles solicitados
        return df.tail(total_limit).reset_index(drop=True)
    except Exception as e:
        raise ValueError(f"Erro ao ler arquivo {file_path}: {str(e)}")
