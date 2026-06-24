# Evo Coin

Projeto Python com dashboard Streamlit, runtime de trading e entrypoint pronto para deploy no Railway.

## Arquivos principais

- `app.py`: dashboard Streamlit
- `bot_runner.py`: runtime principal do bot
- `start_railway.py`: entrypoint usado no Railway
- `config.py`: configuracoes por variavel de ambiente
- `database/database.py`: suporte a SQLite e Postgres
- `services/`: execucao, risco, runtime e integracoes

## Modos de execucao no Railway

O deploy usa `python start_railway.py` e aceita os modos abaixo via `RAILWAY_SERVICE_MODE`:

- `dashboard`: sobe apenas o Streamlit
- `bot`: roda apenas o bot
- `all`: roda dashboard e bot no mesmo container

Para o primeiro deploy, o mais seguro e usar:

```env
RAILWAY_SERVICE_MODE=dashboard
TESTNET=true
ENABLE_LIVE_EXECUTION=false
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP=false
```

## Variaveis de ambiente

Use `.env.example` como referencia de configuracao e replique as variaveis no Railway.

Variaveis mais importantes:

- `RAILWAY_SERVICE_MODE`
- `DATABASE_URL`
- `ADMIN_PANEL_PASSWORD`
- `TESTNET`
- `SYMBOL`
- `TIMEFRAME`
- `BINANCE_TESTNET_API_KEY`
- `BINANCE_TESTNET_SECRET_KEY`
- `BINANCE_API_KEY`
- `BINANCE_SECRET_KEY`
- `ENABLE_LIVE_EXECUTION`
- `LIVE_TRADING_CONFIRMATION`

## O que precisa ir para o GitHub

O repositorio ja esta ajustado para nao subir estado local, bancos, logs e relatorios gerados em massa.

Se voce quiser rodar o bot no Railway, vale manter versionados estes arquivos de apoio:

- `reports/validation/symbol_approvals.json`
- `reports/validation/symbol_strategy_overrides.json`
- `reports/validation/saved_strategy_profiles.json`
- `data/models/runtime_model.tflite`
- `data/models/runtime_model_metadata.json`

Sem os arquivos de `reports/validation`, o dashboard sobe normalmente, mas o runtime do bot pode ficar bloqueado pela governanca de simbolo.

## Passo a passo para subir no Railway

1. Suba o projeto para um repositorio no GitHub.
2. No Railway, crie um projeto novo a partir do repositorio.
3. Adicione um banco Postgres no Railway se quiser persistencia real e copie a `DATABASE_URL`.
4. Cadastre no Railway as variaveis do arquivo `.env.example`.
5. Escolha o modo em `RAILWAY_SERVICE_MODE`.
6. Faca o primeiro deploy em `TESTNET=true`.
7. So habilite `ENABLE_LIVE_EXECUTION=true` quando as credenciais e os limites de risco estiverem revisados.

## Execucao local

Dashboard:

```bash
python start_railway.py
```

Bot:

```bash
set RAILWAY_SERVICE_MODE=bot
python start_railway.py
```

Backtest:

```bash
python backtest.py --candles 3000
```
