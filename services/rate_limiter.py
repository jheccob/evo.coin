import logging
import time

try:
    import aioredis
except ImportError:
    aioredis = None

from config import ProductionConfig

logger = logging.getLogger(__name__)


class RateLimiter:
    """Rate limiter with Redis when available and in-memory fallback otherwise."""

    def __init__(self):
        self.redis_client = None
        self.memory_store = {}
        self._warned_backend = False
        self.limits = {
            "free": {"requests": 3, "window": 3600},
            "premium": {"requests": 1000, "window": 3600},
            "admin": {"requests": 10000, "window": 3600}
        }

    def _warn_backend(self, reason: str):
        if not self._warned_backend:
            logger.warning("RateLimiter usando fallback em memoria: %s", reason)
            self._warned_backend = True

    async def initialize(self):
        """Inicializar conexão Redis quando configurada."""
        if self.redis_client is not None:
            return

        if not aioredis:
            self._warn_backend("aioredis nao esta instalado")
            return

        if not ProductionConfig.REDIS_URL:
            self._warn_backend("REDIS_URL nao configurado")
            return

        try:
            self.redis_client = await aioredis.from_url(ProductionConfig.REDIS_URL)
        except Exception as e:
            self.redis_client = None
            self._warn_backend(str(e))

    def _get_limit_config(self, user_plan: str):
        return self.limits.get(user_plan, self.limits["free"])

    def _get_window_key(self, user_id: int, window: int):
        window_start = int(time.time() // window) * window
        return f"rate_limit:{user_id}:{window_start}", window_start

    async def check_rate_limit(self, user_id: int, user_plan: str = "free") -> bool:
        """Verificar rate limit para usuário."""
        if not self.redis_client:
            await self.initialize()

        limit_config = self._get_limit_config(user_plan)
        key, window_start = self._get_window_key(user_id, limit_config["window"])

        if self.redis_client:
            current_requests = await self.redis_client.incr(key)
            if current_requests == 1:
                await self.redis_client.expire(key, limit_config["window"])
            return current_requests <= limit_config["requests"]

        now = time.time()
        expires_at = window_start + limit_config["window"]
        stored = self.memory_store.get(key)
        if stored and stored["expires_at"] <= now:
            stored = None

        current_requests = 1 if stored is None else stored["count"] + 1
        self.memory_store[key] = {
            "count": current_requests,
            "expires_at": expires_at
        }
        return current_requests <= limit_config["requests"]

    async def get_remaining_requests(self, user_id: int, user_plan: str = "free") -> int:
        """Obter requests restantes."""
        if not self.redis_client:
            await self.initialize()

        limit_config = self._get_limit_config(user_plan)
        key, _ = self._get_window_key(user_id, limit_config["window"])

        if self.redis_client:
            current_requests = await self.redis_client.get(key)
            if not current_requests:
                return limit_config["requests"]
            return max(0, limit_config["requests"] - int(current_requests))

        stored = self.memory_store.get(key)
        if not stored or stored["expires_at"] <= time.time():
            return limit_config["requests"]

        return max(0, limit_config["requests"] - int(stored["count"]))
