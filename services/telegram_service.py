"""
Servico seguro do Telegram.
"""

import logging
import os
from datetime import datetime
from typing import Any, Dict, Tuple

logger = logging.getLogger(__name__)

try:
    from telegram import Bot
    import telegram

    TELEGRAM_AVAILABLE = True
    logger.info("Telegram service usando v%s", telegram.__version__)
except ImportError as e:
    TELEGRAM_AVAILABLE = False
    Bot = None
    logger.warning("Telegram nao disponivel no service: %s", e)


class SecureTelegramService:
    def __init__(self):
        self.bot = None
        self.chat_id = None
        self._configured = False

        self._load_from_environment()

    def _load_from_environment(self):
        """Carrega configuracao do Telegram a partir do ambiente."""
        if not TELEGRAM_AVAILABLE:
            return

        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            return

        try:
            self.bot = Bot(token=token)
            self.chat_id = chat_id
            self._configured = True
            logger.info("Telegram configurado via variaveis de ambiente")
        except Exception as e:
            logger.error("Erro ao configurar Telegram via ambiente: %s", e)
            self._configured = False

    def configure(self, bot_token: str, chat_id: str) -> Tuple[bool, str]:
        """Configura o bot do Telegram para a sessao atual."""
        if not TELEGRAM_AVAILABLE:
            return False, "Biblioteca python-telegram-bot nao disponivel"

        try:
            if not bot_token or not chat_id:
                return False, "Token e Chat ID sao obrigatorios"
            if ":" not in bot_token:
                return False, "Formato de token invalido"

            self.bot = Bot(token=bot_token)
            self.chat_id = chat_id
            self._configured = True

            logger.info("Telegram configurado para a sessao atual")
            return True, "Configuracao aplicada nesta sessao. Para persistir, use variaveis de ambiente."
        except Exception as e:
            logger.error("Erro na configuracao do Telegram: %s", e)
            return False, f"Erro na configuracao: {str(e)}"

    def is_configured(self) -> bool:
        return self._configured and self.bot is not None and self.chat_id is not None

    def get_config_status(self) -> Dict[str, Any]:
        return {
            "configured": self.is_configured(),
            "has_bot": self.bot is not None,
            "has_chat_id": self.chat_id is not None,
            "telegram_available": TELEGRAM_AVAILABLE,
        }

    def disable(self):
        self.bot = None
        self.chat_id = None
        self._configured = False
        logger.info("Telegram desabilitado")

    async def test_connection(self) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "Telegram nao configurado"

        try:
            bot_info = await self.bot.get_me()
            test_message = f"Teste de conexao - {datetime.now().strftime('%H:%M:%S')}"
            await self.bot.send_message(chat_id=self.chat_id, text=test_message)
            return True, f"Conectado como @{bot_info.username}"
        except Exception as e:
            logger.error("Erro no teste do Telegram: %s", e)
            return False, f"Erro na conexao: {str(e)}"

    async def send_signal_alert(
        self,
        symbol: str,
        signal: str,
        price: float,
        rsi: float,
        macd: float,
        macd_signal: float,
    ) -> bool:
        if not self.is_configured():
            return False

        try:
            signal_emojis = {
                "COMPRA": "BUY",
                "VENDA": "SELL",
                "NEUTRO": "WAIT",
            }
            signal_label = signal_emojis.get(signal, signal)
            message = (
                f"{signal_label}\n\n"
                f"Par: {symbol}\n"
                f"Preco: ${price:.6f}\n"
                f"RSI: {rsi:.2f}\n"
                f"MACD: {macd:.4f}\n"
                f"Signal: {macd_signal:.4f}\n\n"
                f"{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
            )
            await self.bot.send_message(chat_id=self.chat_id, text=message)
            logger.info("Sinal enviado: %s para %s", signal, symbol)
            return True
        except Exception as e:
            logger.error("Erro ao enviar sinal: %s", e)
            return False

    async def send_custom_message(self, message: str) -> Tuple[bool, str]:
        if not self.is_configured():
            return False, "Telegram nao configurado"

        try:
            await self.bot.send_message(chat_id=self.chat_id, text=message)
            return True, "Mensagem enviada"
        except Exception as e:
            logger.error("Erro ao enviar mensagem: %s", e)
            return False, f"Erro: {str(e)}"
