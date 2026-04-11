#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
APP_USER="${SUDO_USER:-$USER}"
VENV_DIR="$APP_DIR/.venv"
PYTHON_BIN="$VENV_DIR/bin/python"
ENV_FILE="/etc/trading-bot.env"
SERVICE_TEMPLATE="$APP_DIR/deploy/gcp/trading-bot.service"
SERVICE_FILE="/etc/systemd/system/trading-bot.service"

echo "==> Atualizando pacotes base"
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git

echo "==> Criando ambiente virtual em $VENV_DIR"
python3 -m venv "$VENV_DIR"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install -r "$APP_DIR/requirements_railway.txt"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "==> Criando arquivo de ambiente em $ENV_FILE"
  sudo install -m 600 "$APP_DIR/deploy/gcp/trading-bot.env.example" "$ENV_FILE"
  echo "Preencha TELEGRAM_BOT_TOKEN em $ENV_FILE antes de iniciar o servico."
fi

echo "==> Instalando unit file do systemd"
TMP_SERVICE="$(mktemp)"
sed \
  -e "s|__APP_USER__|$APP_USER|g" \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  -e "s|__PYTHON_BIN__|$PYTHON_BIN|g" \
  -e "s|__ENV_FILE__|$ENV_FILE|g" \
  "$SERVICE_TEMPLATE" > "$TMP_SERVICE"

sudo install -m 644 "$TMP_SERVICE" "$SERVICE_FILE"
rm -f "$TMP_SERVICE"

sudo systemctl daemon-reload
sudo systemctl enable trading-bot.service

if grep -q '^TELEGRAM_BOT_TOKEN=$' "$ENV_FILE"; then
  echo "==> Servico instalado, mas ainda sem token."
  echo "Edite $ENV_FILE e depois execute:"
  echo "sudo systemctl restart trading-bot.service"
else
  echo "==> Reiniciando servico"
  sudo systemctl restart trading-bot.service
fi

echo "==> Comandos uteis"
echo "sudo systemctl status trading-bot.service"
echo "sudo journalctl -u trading-bot.service -f"
