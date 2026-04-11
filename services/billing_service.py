import logging
from typing import Dict, Optional

try:
    import stripe
except ImportError:
    stripe = None

from config import ProductionConfig
from user_manager import UserManager

logger = logging.getLogger(__name__)


class BillingService:
    """Billing service with a safe fallback when Stripe is not configured."""

    def __init__(self):
        self.user_manager = UserManager()
        self.webhook_secret = ProductionConfig.STRIPE_WEBHOOK_SECRET
        self.enabled = bool(
            stripe
            and ProductionConfig.STRIPE_SECRET_KEY
            and self.webhook_secret
            and ProductionConfig.STRIPE_SUCCESS_URL
            and ProductionConfig.STRIPE_CANCEL_URL
            and ProductionConfig.PREMIUM_PRICE_MONTHLY > 0
        )

        if self.enabled:
            stripe.api_key = ProductionConfig.STRIPE_SECRET_KEY
        else:
            logger.warning("BillingService iniciado sem configuracao completa do Stripe")

    def _extract_user_id(self, payload: Dict) -> Optional[int]:
        metadata = payload.get("metadata") or {}
        raw_user_id = metadata.get("telegram_user_id")
        if raw_user_id and str(raw_user_id).isdigit():
            return int(raw_user_id)
        return None

    async def create_payment_link(self, user_id: int) -> str:
        """Criar link de pagamento."""
        if not self.enabled:
            return (
                "Billing indisponivel: configure STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET, "
                "STRIPE_SUCCESS_URL, STRIPE_CANCEL_URL e PREMIUM_PRICE_MONTHLY"
            )

        try:
            customer = stripe.Customer.create(
                metadata={"telegram_user_id": str(user_id)}
            )

            session = stripe.checkout.Session.create(
                customer=customer.id,
                payment_method_types=["card"],
                line_items=[{
                    "price_data": {
                        "currency": "brl",
                        "product_data": {
                            "name": "Trading Bot Premium",
                            "description": "Analises ilimitadas + Alertas em tempo real"
                        },
                        "unit_amount": int(ProductionConfig.PREMIUM_PRICE_MONTHLY * 100),
                        "recurring": {"interval": "month"}
                    },
                    "quantity": 1,
                }],
                mode="subscription",
                success_url=ProductionConfig.STRIPE_SUCCESS_URL,
                cancel_url=ProductionConfig.STRIPE_CANCEL_URL,
                metadata={"telegram_user_id": str(user_id)}
            )

            return session.url

        except Exception as e:
            logger.error("Erro ao criar link de pagamento: %s", e)
            return "Erro ao gerar link de pagamento"

    async def handle_webhook(self, payload: str, signature: str) -> bool:
        """Processar webhook do Stripe."""
        if not self.enabled:
            logger.warning("Webhook recebido, mas billing nao esta configurado")
            return False

        try:
            event = stripe.Webhook.construct_event(
                payload, signature, self.webhook_secret
            )

            if event["type"] == "checkout.session.completed":
                await self.handle_subscription_created(event["data"]["object"])
            elif event["type"] == "invoice.payment_succeeded":
                await self.handle_payment_succeeded(event["data"]["object"])
            elif event["type"] == "customer.subscription.deleted":
                await self.handle_subscription_cancelled(event["data"]["object"])

            return True

        except Exception as e:
            logger.error("Erro no webhook: %s", e)
            return False

    async def handle_subscription_created(self, session):
        """Processar nova assinatura."""
        user_id = self._extract_user_id(session)
        if user_id is None:
            logger.warning("Checkout sem telegram_user_id no metadata")
            return

        self.user_manager.upgrade_to_premium(user_id)
        logger.info("Nova assinatura criada para usuario %s", user_id)

    async def handle_payment_succeeded(self, invoice):
        """Processar pagamento recorrente aprovado."""
        user_id = self._extract_user_id(invoice)
        if user_id is not None:
            self.user_manager.upgrade_to_premium(user_id)
            logger.info("Pagamento confirmado para usuario %s", user_id)

    async def handle_subscription_cancelled(self, subscription):
        """Processar cancelamento de assinatura."""
        user_id = self._extract_user_id(subscription)
        if user_id is not None:
            self.user_manager.set_user_plan(user_id, "free")
            logger.info("Assinatura cancelada para usuario %s", user_id)

    async def is_user_premium(self, user_id: int) -> bool:
        """Verificar se usuario e premium."""
        return self.user_manager.is_premium(user_id)

    async def get_active_subscription(self, user_id: int) -> Optional[Dict]:
        """Obter assinatura ativa do usuario."""
        user = self.user_manager.get_user(user_id)
        if not user or user.get("plan") != "premium":
            return None

        return {
            "user_id": user_id,
            "status": "active",
            "plan": user.get("plan")
        }
