import pandas as pd
import gzip
import os


def get_exchange(testnet: bool = False):
    import ccxt
    config = {"enableRateLimit": True}

    # Prefer futures-native client when available.
    exchange_cls = getattr(ccxt, "binanceusdm", None)
    if exchange_cls is None:
        config["options"] = {"defaultType": "future"}
        exchange = ccxt.binance(config)
    else:
        exchange = exchange_cls(config)

    if testnet:
        exchange.set_sandbox_mode(True)
    return exchange


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


def _candles_to_dataframe(ohlcv):
    df = pd.DataFrame(
        ohlcv,
        columns=["timestamp", "open", "high", "low", "close", "volume"],
    )
    for column in ["open", "high", "low", "close", "volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def fetch_candles(symbol: str, timeframe: str, limit: int = 500, testnet: bool = False):
    exchange = get_exchange(testnet=testnet)
    resolved_symbol = _resolve_exchange_symbol(exchange, symbol)
    ohlcv = exchange.fetch_ohlcv(resolved_symbol, timeframe=timeframe, limit=limit)
    return _candles_to_dataframe(ohlcv)


def fetch_historical_candles(symbol: str, timeframe: str, total_limit: int = 2000, batch_limit: int = 500, testnet: bool = False):
    exchange = get_exchange(testnet=testnet)
    resolved_symbol = _resolve_exchange_symbol(exchange, symbol)
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
    return _candles_to_dataframe(unique[-total_limit:])


def _normalize_symbol_for_csv(symbol: str) -> str:
    return symbol.replace("/", "").replace("-", "").replace(":", "").upper()


def _normalize_timeframe_for_csv(timeframe: str) -> str:
    return timeframe.replace(" ", "").lower()


def fetch_historical_candles_from_csv(symbol: str, timeframe: str, total_limit: int = 2000):
    """
    Lê dados históricos de um arquivo CSV local gzipado.
    Exemplo: BTCUSDT_15m.csv.gz localizado em data/history/
    """
    symbol = _normalize_symbol_for_csv(symbol)
    timeframe = _normalize_timeframe_for_csv(timeframe)
    file_name = f"{symbol}_{timeframe}.csv.gz"
    file_path = os.path.join("data", "history", file_name)
    
    if not os.path.exists(file_path):
        raise FileNotFoundError(f"Arquivo não encontrado: {file_path}")
    
    try:
        df = pd.read_csv(gzip.open(file_path, 'rt'))
        df['timestamp'] = pd.to_datetime(df['timestamp'], utc=True)
        
        # Converter colunas numéricas
        for column in ['open', 'high', 'low', 'close', 'volume']:
            df[column] = pd.to_numeric(df[column], errors='coerce')
        
        # Retornar os últimos N candles solicitados
        return df.tail(total_limit).reset_index(drop=True)
    except Exception as e:
        raise ValueError(f"Erro ao ler arquivo {file_path}: {str(e)}")
