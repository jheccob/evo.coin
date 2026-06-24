# Deploy no Railway

Guia prático para subir o projeto no Railway com banco PostgreSQL.

## Arquitetura recomendada

Use 3 serviços no mesmo projeto Railway:

1. `evo-postgres`
   Banco PostgreSQL gerenciado pelo Railway.
2. `evo-dashboard`
   Serviço público com `RAILWAY_SERVICE_MODE=dashboard`.
3. `evo-bot`
   Serviço privado com `RAILWAY_SERVICE_MODE=bot`.

Essa separação é mais estável do que usar `RAILWAY_SERVICE_MODE=all`, porque o dashboard e o bot ficam independentes.

## O que o projeto já faz

- O Railway já vai usar o `Dockerfile` da raiz.
- O start command já está configurado em `railway.json` para `python start_railway.py`.
- O banco é inicializado automaticamente no boot.
- Se `DATABASE_URL` apontar para Postgres, o projeto usa Postgres.
- Se `DATABASE_URL` não existir, ele cai para SQLite local.

No Railway, prefira sempre Postgres. Não use SQLite em produção sem volume persistente.

## Ordem correta de configuração

1. Criar um novo projeto no Railway a partir do repositório GitHub.
2. Adicionar o serviço PostgreSQL no mesmo projeto.
3. Criar ou duplicar um serviço de app para o dashboard.
4. Criar ou duplicar um segundo serviço de app para o bot.
5. Em ambos os serviços de app, adicionar `DATABASE_URL` como reference variable do serviço Postgres.
6. Configurar as variáveis do dashboard.
7. Configurar as variáveis do bot.
8. Fazer o primeiro deploy em `TESTNET=true`.
9. Gerar domínio público apenas para o dashboard.
10. Validar login, conexão com banco e status do runtime.

## Serviço 1: PostgreSQL

No canvas do Railway:

1. Clique em `New`.
2. Escolha `Database`.
3. Escolha `PostgreSQL`.

Depois disso o Railway cria variáveis como:

- `PGHOST`
- `PGPORT`
- `PGUSER`
- `PGPASSWORD`
- `PGDATABASE`
- `DATABASE_URL`

No app, a variável que interessa é `DATABASE_URL`.

## Serviço 2: Dashboard

Crie um serviço conectado ao mesmo repositório e branch `main`.

### Variáveis obrigatórias do dashboard

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

### Variáveis opcionais do dashboard

```env
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
CREDENTIAL_ENCRYPTION_KEY=
```

Se quiser salvar credenciais da exchange pelo dashboard com criptografia, defina `CREDENTIAL_ENCRYPTION_KEY`.

## Serviço 3: Bot

Crie outro serviço no mesmo repositório.

### Variáveis obrigatórias do bot

```env
RAILWAY_SERVICE_MODE=bot
DATABASE_URL=<reference variable do Postgres>
TESTNET=true
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
```

### Credenciais para rodar em testnet

```env
BINANCE_TESTNET_API_KEY=...
BINANCE_TESTNET_SECRET_KEY=...
```

### Credenciais para rodar em conta real

```env
BINANCE_API_KEY=...
BINANCE_SECRET_KEY=...
ENABLE_LIVE_EXECUTION=true
LIVE_TRADING_CONFIRMATION=EU_ASSUMO_RISCO
TESTNET=false
```

Só vire essas variáveis para produção depois que a testnet estiver estável.

## Como ligar o banco corretamente

Não copie uma URL manual se puder evitar.

No Railway:

1. Entre no serviço `evo-dashboard`.
2. Abra a aba `Variables`.
3. Clique em `Add Reference Variable`.
4. Escolha o serviço Postgres.
5. Selecione `DATABASE_URL`.

Repita a mesma coisa no `evo-bot`.

Assim a variável continua sincronizada se o Railway trocar credenciais internas do banco.

## Migração e criação de tabelas

Não existe um passo manual de migration separado neste projeto.

O arquivo `database/database.py` chama `init_database()` no startup e executa vários `CREATE TABLE IF NOT EXISTS`.

Na prática:

- subiu com `DATABASE_URL` correto
- conectou no Postgres
- as tabelas são criadas automaticamente

## Domínio público

Gere domínio apenas no dashboard.

No serviço `evo-dashboard`:

1. Abra `Settings`.
2. Vá em `Networking`.
3. Em `Public Networking`, clique em `Generate Domain`.

O bot não precisa de domínio público.

## Primeira subida recomendada

Faça assim:

1. Suba o `evo-dashboard` com `TESTNET=true`.
2. Abra o domínio gerado e confirme que o Streamlit carregou.
3. Verifique no dashboard se o backend mostra Postgres em vez de SQLite.
4. Suba o `evo-bot` ainda em `TESTNET=true`.
5. Confira os logs do bot e veja se ele inicializa sem erro de governança, banco ou credenciais.

## Checklist de validação depois do deploy

### Dashboard

- o domínio abre
- o login admin funciona
- o dashboard não mostra erro de `ADMIN_PANEL_PASSWORD`
- o banco aparece como Postgres

### Banco

- `DATABASE_URL` está definido por reference variable
- o serviço Postgres está `healthy`
- o app não está caindo para `sqlite`

### Bot

- logs mostram boot normal
- não há erro de `DATABASE_URL`
- não há erro de `BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP`
- não há erro de credencial Binance
- não há erro de símbolo bloqueado por governança

## Variáveis opcionais que você não precisa no primeiro deploy

Pode deixar vazio no começo:

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

## Configuração mínima para funcionar hoje

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
BINANCE_TESTNET_API_KEY=<sua-chave>
BINANCE_TESTNET_SECRET_KEY=<seu-secret>
BOT_REQUIRE_LOCAL_CSV_BOOTSTRAP=false
```

## Alternativa simples

Se quiser começar mais rápido, dá para usar:

- 1 serviço Postgres
- 1 serviço app com `RAILWAY_SERVICE_MODE=all`

Funciona, mas não é o melhor desenho para operação contínua. O recomendado continua sendo separar dashboard e bot.
