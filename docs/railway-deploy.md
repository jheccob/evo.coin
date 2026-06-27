# Deploy no Railway

Guia pratico para subir o projeto no Railway com PostgreSQL e arquitetura separada para dashboard e bot.

## Arquitetura recomendada

Use 3 servicos no mesmo projeto Railway:

1. `evo-postgres`
   Banco PostgreSQL gerenciado pelo Railway.
2. `evo-dashboard`
   Servico publico com `RAILWAY_SERVICE_MODE=dashboard`.
3. `evo-bot`
   Servico privado com `RAILWAY_SERVICE_MODE=bot`.

Essa separacao e mais estavel do que usar `RAILWAY_SERVICE_MODE=all`, porque dashboard e bot ficam independentes.

No modelo multiusuario:

- a dashboard publica grava comandos de start e stop no banco
- o servico privado `evo-bot` reconcilia esses comandos em background
- cada conta ativa roda em contexto isolado

## O que o projeto ja faz

- o Railway usa o `Dockerfile` da raiz
- o start command ja esta em `railway.json` como `python start_railway.py`
- o banco e inicializado automaticamente no boot
- se `DATABASE_URL` apontar para Postgres, o projeto usa Postgres
- se `DATABASE_URL` nao existir, ele cai para SQLite local

No Railway, prefira sempre Postgres. Nao use SQLite em producao sem volume persistente.

Sobre porta:

- localmente, este projeto usa `8080` como fallback
- no Railway, a aplicacao deve escutar a `PORT` injetada pela plataforma
- se `PORT` existir, ela sempre tem prioridade sobre o fallback local

## Ordem correta de configuracao

1. Criar um novo projeto no Railway a partir do repositorio GitHub.
2. Adicionar o servico PostgreSQL no mesmo projeto.
3. Criar ou duplicar um servico de app para o dashboard.
4. Criar ou duplicar um segundo servico de app para o bot.
5. Nos dois servicos de app, adicionar `DATABASE_URL` como reference variable do Postgres.
6. Configurar as variaveis do dashboard.
7. Configurar as variaveis do bot.
8. Fazer o primeiro deploy em `TESTNET=true`.
9. Gerar dominio publico apenas para o dashboard.
10. Validar login, conexao com banco e status do runtime.

## Servico 1: PostgreSQL

No canvas do Railway:

1. Clique em `New`.
2. Escolha `Database`.
3. Escolha `PostgreSQL`.

Depois disso o Railway cria variaveis como:

- `PGHOST`
- `PGPORT`
- `PGUSER`
- `PGPASSWORD`
- `PGDATABASE`
- `DATABASE_URL`

No app, a variavel que interessa e `DATABASE_URL`.

## Servico 2: Dashboard

Crie um servico conectado ao mesmo repositorio e branch `main`.

### Variaveis obrigatorias do dashboard

```env
RAILWAY_SERVICE_MODE=dashboard
DATABASE_URL=<reference variable do Postgres>
TESTNET=true
ADMIN_PANEL_PASSWORD=troque-por-uma-senha-forte
ALLOW_SELF_SERVICE_SIGNUP=false
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP=false
BOT_ALLOW_REST_FALLBACK=true
SYMBOL=BTC/USDT
TIMEFRAME=15m
SINGLE_USER_RUNTIME_EXCHANGE=binanceusdm
SINGLE_USER_RUNTIME_ACCOUNT_ID=railway-primary
SINGLE_USER_RUNTIME_ACCOUNT_ALIAS=Railway Primary
ENABLE_LIVE_EXECUTION=false
RUNTIME_REQUIRE_APPROVED_SYMBOL=true
RUNTIME_SYMBOL_APPROVAL_OVERRIDE=false
ENABLE_AI_ASSISTANT=true
AI_MODEL_PATH=data/models/runtime_model.tflite
AI_MODEL_METADATA_PATH=data/models/runtime_model_metadata.json
```

### Variaveis opcionais do dashboard

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CREDENTIAL_ENCRYPTION_KEY=
```

Se quiser salvar credenciais da exchange pelo dashboard com criptografia, defina `CREDENTIAL_ENCRYPTION_KEY`.

## Servico 3: Bot

Crie outro servico no mesmo repositorio.

### Variaveis obrigatorias do bot

```env
RAILWAY_SERVICE_MODE=bot
DATABASE_URL=<reference variable do Postgres>
TESTNET=true
ENABLE_MULTIUSER_RUNTIME=true
SYMBOL=BTC/USDT
TIMEFRAME=15m
SINGLE_USER_RUNTIME_EXCHANGE=binanceusdm
SINGLE_USER_RUNTIME_ACCOUNT_ID=railway-primary
SINGLE_USER_RUNTIME_ACCOUNT_ALIAS=Railway Primary
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP=false
BOT_ALLOW_REST_FALLBACK=true
ENABLE_LIVE_EXECUTION=false
RUNTIME_REQUIRE_APPROVED_SYMBOL=true
RUNTIME_SYMBOL_APPROVAL_OVERRIDE=false
ENABLE_AI_ASSISTANT=true
AI_MODEL_PATH=data/models/runtime_model.tflite
AI_MODEL_METADATA_PATH=data/models/runtime_model_metadata.json
CREDENTIAL_ENCRYPTION_KEY=<mesma-chave-do-dashboard-se-usar-vault>
```

### Credenciais em modo multiusuario

Se os usuarios vao salvar as proprias chaves pela dashboard:

- defina a mesma `CREDENTIAL_ENCRYPTION_KEY` no `evo-dashboard` e no `evo-bot`
- deixe `ENABLE_MULTIUSER_RUNTIME=true` no servico `evo-bot`
- use a aba `Runtime` do workspace para enviar start e stop remoto por conta

Observacao importante:

- para o fluxo do cliente final, voce pode operar com uma unica conta logica por usuario
- essa conta guarda um unico par de chaves reais por `user_id + account_id + exchange`
- testnet pode ficar apenas para uso interno ou administracao, se voce ainda quiser manter esse ambiente

### Credenciais para modo single-user ou fallback

#### Testnet

```env
BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_SECRET_KEY=...
```

#### Conta real

```env
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
ENABLE_LIVE_EXECUTION=true
LIVE_TRADING_CONFIRMATION=EU_ASSUMO_RISCO
TESTNET=false
```

So vire essas variaveis para producao depois que a testnet estiver estavel.

## Como ligar o banco corretamente

Nao copie uma URL manual se puder evitar.

No Railway:

1. Entre no servico `evo-dashboard`.
2. Abra a aba `Variables`.
3. Clique em `Add Reference Variable`.
4. Escolha o servico Postgres.
5. Selecione `DATABASE_URL`.

Repita a mesma coisa no `evo-bot`.

Assim a variavel continua sincronizada se o Railway trocar credenciais internas do banco.

## Migracao e criacao de tabelas

Nao existe um passo manual de migration separado neste projeto.

O arquivo `database/database.py` chama `init_database()` no startup e executa varios `CREATE TABLE IF NOT EXISTS`.

Na pratica:

- subiu com `DATABASE_URL` correto
- conectou no Postgres
- as tabelas sao criadas automaticamente

## Dominio publico

Gere dominio apenas no dashboard.

No servico `evo-dashboard`:

1. Abra `Settings`.
2. Va em `Networking`.
3. Em `Public Networking`, clique em `Generate Domain`.

O bot nao precisa de dominio publico.

## Primeira subida recomendada

Faca assim:

1. Suba o `evo-dashboard` com `TESTNET=true`.
2. Abra o dominio gerado e confirme que o Streamlit carregou.
3. Verifique no dashboard se o backend mostra Postgres em vez de SQLite.
4. Suba o `evo-bot` ainda em `TESTNET=true`.
5. Confira os logs do bot e veja se ele inicializa sem erro de governanca, banco ou credenciais.
6. Entre no workspace, salve credenciais criptografadas reais e use a aba `Runtime` da conta para enviar um start remoto.

## Checklist de validacao depois do deploy

### Dashboard

- o dominio abre
- o login admin funciona
- o dashboard nao mostra erro de `ADMIN_PANEL_PASSWORD`
- o banco aparece como Postgres

### Banco

- `DATABASE_URL` esta definido por reference variable
- o servico Postgres esta `healthy`
- o app nao esta caindo para `sqlite`

### Bot

- logs mostram boot normal
- nao ha erro de `DATABASE_URL`
- `ENABLE_MULTIUSER_RUNTIME=true` esta definido se o bot for operar contas do workspace
- nao ha erro de `BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP`
- nao ha erro de credencial Binance
- nao ha erro de simbolo bloqueado por governanca

## Variaveis opcionais que voce nao precisa no primeiro deploy

Pode deixar vazio no comeco:

- `REDIS_URL`
- `STRIPE_SECRET_KEY`
- `STRIPE_WEBHOOK_SECRET`
- `STRIPE_SUCCESS_URL`
- `STRIPE_CANCEL_URL`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Como gerar uma chave para o vault

Se quiser guardar credenciais com criptografia no banco, gere uma chave Fernet:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Use o valor gerado em:

```env
CREDENTIAL_ENCRYPTION_KEY=<chave-gerada>
```

Se usar vault, coloque a mesma chave no dashboard e no bot.

## Configuracao minima para funcionar hoje

### Dashboard

```env
RAILWAY_SERVICE_MODE=dashboard
DATABASE_URL=<reference variable do Postgres>
TESTNET=true
ADMIN_PANEL_PASSWORD=<senha-forte>
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP=false
```

### Bot

```env
RAILWAY_SERVICE_MODE=bot
DATABASE_URL=<reference variable do Postgres>
TESTNET=true
ENABLE_MULTIUSER_RUNTIME=true
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP=false
```

## Alternativa simples

Se quiser comecar mais rapido, da para usar:

- 1 servico Postgres
- 1 servico app com `RAILWAY_SERVICE_MODE=all`

Funciona, mas nao e o melhor desenho para operacao continua. O recomendado continua sendo separar dashboard e bot.
