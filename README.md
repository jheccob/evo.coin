# Refatoração limpa v2

Atualizei a base para a sua configuração real, sem `logger.py` e sem depender dos módulos antigos que você apagou.

## O que foi ajustado
- `config.py` já veio preenchido com os valores que você me enviou.
- `strategy_engine.py` agora usa esses valores como padrão.
- `backtest.py` usa seu símbolo e timeframe por padrão.
- `bot_runner.py` substitui o seu `main` antigo sem depender de `logger.py`.
- `futures_trading_refactored.py` ficou alinhado com o novo `config.py`.

## Arquivos principais
- `config.py`
- `market_data.py`
- `strategy_engine.py`
- `position_manager.py`
- `risk_management_service.py`
- `backtest.py`
- `bot_runner.py`
- `futures_trading_refactored.py`

## Observação importante
`bot_runner.py` ainda é um runner estrutural para teste local e paper logic. Para conta real ainda falta a camada de envio real de ordens da Binance com:
- criação de ordem
- stop real na exchange
- take profit real
- reconciliação de posição
- tratamento de erro da exchange

## Como testar
Backtest:
```bash
python backtest.py --candles 3000
```

Runner local:
```bash
python bot_runner.py
```
