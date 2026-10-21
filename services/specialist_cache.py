# services/specialist_cache.py
import json
import logging
from typing import Sequence
from services.cache_service import cache_json_get, cache_json_set, cache_delete
from services.support_service import get_support_service

logger = logging.getLogger(__name__)

CACHE_KEY = "cache:support:online"
CACHE_TTL = 60


async def get_online_specialists_cached() -> list[dict]:
    """
    Возвращает список онлайн-специалистов.
    Кэш 60 секунд — при изменении статуса инвалидируется.
    """
    cached = await cache_json_get(CACHE_KEY)
    if cached is not None:
        return cached

    svc = get_support_service()
    specialists = await svc.get_online_specialist()
    data = [
        {"support_id": str(s.support_id), "full_name": s.full_name, "status": s.status.value}
        for s in specialists
    ]
    await cache_json_set(CACHE_KEY, data, ttl=CACHE_TTL)
    return data


async def invalidate_online_cache() -> None:
    """Вызывается при /online, /offline, смене статуса супервизором."""
    await cache_delete(CACHE_KEY)