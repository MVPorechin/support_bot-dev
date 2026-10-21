"""
Кэш chat_id специалистов.

Хранит в Redis: cache:spec_chat_id:<huid> → chat_id (UUID string).
TTL 300 секунд. При промахе — читает из БД и заполняет кэш.
"""
import logging
import uuid
from typing import Optional

from services.cache_service import (
    cache_get,
    cache_set,
    cache_set_if_absent,
    cache_delete,
)
from services.support_service import SupportService

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "cache:spec_chat_id"
LOCK_KEY_PREFIX = "lock:spec_chat_id_load"
CACHE_TTL_SEC = 300
LOAD_LOCK_TTL_SEC = 10


def _cache_key(huid: uuid.UUID) -> str:
    return f"{CACHE_KEY_PREFIX}:{huid}"


def _lock_key(huid: uuid.UUID) -> str:
    return f"{LOCK_KEY_PREFIX}:{huid}"


async def get_cached_chat_id(
    support_huid: uuid.UUID,
) -> Optional[uuid.UUID]:
    raw = await cache_get(_cache_key(support_huid))
    if raw is None:
        return None
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError):
        logger.warning(f"[SPEC_CACHE] Битый chat_id в кэше: {raw}")
        await cache_delete(_cache_key(support_huid))
        return None


async def set_cached_chat_id(
    support_huid: uuid.UUID,
    chat_id: uuid.UUID,
) -> None:
    await cache_set(
        _cache_key(support_huid),
        str(chat_id),
        ttl=CACHE_TTL_SEC,
    )


async def invalidate_cached_chat_id(
    support_huid: uuid.UUID,
) -> None:
    await cache_delete(_cache_key(support_huid))


async def get_or_load_chat_id(
    support_huid: uuid.UUID,
    incoming_chat_id: uuid.UUID,
) -> Optional[uuid.UUID]:
    """
    Возвращает chat_id специалиста.
    Порядок: Redis → lock → БД → Redis.
    Если у специалиста не было chat_id — заполняет из incoming_chat_id.
    """
    # 1. Кэш
    cached = await get_cached_chat_id(support_huid)
    if cached is not None:
        return cached

    # 2. Lock на загрузку (защита от «шторма» при промахе)
    got_lock = await cache_set_if_absent(
        _lock_key(support_huid),
        "1",
        ttl=LOAD_LOCK_TTL_SEC,
    )
    if not got_lock:
        return await get_cached_chat_id(support_huid)

    # 3. Мы владеем локом
    try:
        svc = SupportService()
        specialist = await svc.repo.get_specialist_by_id(support_huid)
        if specialist is None:
            return None

        if specialist.chat_id is not None:
            await set_cached_chat_id(support_huid, specialist.chat_id)
            return specialist.chat_id

        # Заполняем
        await svc.ensure_chat_id(support_huid, incoming_chat_id)
        await set_cached_chat_id(support_huid, incoming_chat_id)
        logger.info(
            f"[SPEC_CACHE] chat_id заполнен для {support_huid}: "
            f"{incoming_chat_id}"
        )
        return incoming_chat_id

    except Exception as exc:
        logger.exception(
            f"[SPEC_CACHE] Ошибка загрузки chat_id для {support_huid}: {exc}"
        )
        return None