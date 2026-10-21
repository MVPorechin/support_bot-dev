"""Распределённый lock через Redis SET NX EX."""
import logging
from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RedisLock:
    """
    Простой распределённый lock.
    Не освобождается явно — истекает по TTL.
    Это защищает от race, если процесс упал между acquire и release.
    """

    def __init__(self, redis: Redis, key: str, ttl: int):
        self.redis = redis
        self.key = f"lock:{key}"
        self.ttl = ttl
        self.acquired = False

    async def __aenter__(self) -> bool:
        try:
            self.acquired = bool(
                await self.redis.set(self.key, "1", ex=self.ttl, nx=True)
            )
        except Exception as exc:
            logger.error(f"[LOCK] Ошибка acquire {self.key}: {exc}")
            self.acquired = False
        return self.acquired

    async def __aexit__(self, *args) -> None:
        # Намеренно ничего не делаем — TTL сам освободит
        pass