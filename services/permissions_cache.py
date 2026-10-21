"""
Redis-кэш прав: список huid'ов поддержки и супервизоров.

Ключи:
  cache:perms:support_huids       → JSON-массив строк-huid'ов
  cache:perms:supervisor_huids    → JSON-массив строк-huid'ов

TTL: 300 секунд (5 минут).
Инвалидация — при любом изменении состава:
  • регистрация/активация специалиста
  • /online, /offline, /break (если хотите пересчёт)
  • регистрация/деактивация супервизора
"""
import logging

from services.cache_service import (
    cache_json_get,
    cache_json_set,
    cache_delete,
)

logger = logging.getLogger(__name__)

CACHE_TTL_SEC = 300

KEY_SUPPORT = "cache:perms:support_huids"
KEY_SUPERVISOR = "cache:perms:supervisor_huids"


# ══════════════════════════════════════════════════════════════
# SUPPORT (специалисты поддержки)
# ══════════════════════════════════════════════════════════════
async def get_support_huids_cached() -> set[str]:
    """
    Возвращает set строк-huid'ов всех специалистов поддержки.
    Из кэша или из БД (с записью в кэш).
    """
    cached = await cache_json_get(KEY_SUPPORT)
    if cached is not None:
        return set(cached)

    # Cache miss — идём в БД
    from services.support_service import get_support_service

    svc = get_support_service()
    all_huids = await svc.get_all()
    huids_str = [str(h) for h in all_huids]

    await cache_json_set(KEY_SUPPORT, huids_str, ttl=CACHE_TTL_SEC)
    logger.debug(
        f"[PERMS] support_huids загружены из БД: {len(huids_str)} записей"
    )
    return set(huids_str)


async def invalidate_support_cache() -> None:
    """Сбросить кэш поддержки — следующий запрос пойдёт в БД."""
    await cache_delete(KEY_SUPPORT)
    logger.info("[PERMS] Кэш support_huids инвалидирован")


# ══════════════════════════════════════════════════════════════
# SUPERVISOR (руководители)
# ══════════════════════════════════════════════════════════════
async def get_supervisor_huids_cached() -> set[str]:
    """
    Возвращает set строк-huid'ов всех активных супервизоров.
    Из кэша или из БД (с записью в кэш).
    """
    cached = await cache_json_get(KEY_SUPERVISOR)
    if cached is not None:
        return set(cached)

    from services.supervisor_service import get_supervisor_service

    svc = get_supervisor_service()
    all_ids = await svc.get_all_active()
    huids_str = [str(h) for h in all_ids]

    await cache_json_set(KEY_SUPERVISOR, huids_str, ttl=CACHE_TTL_SEC)
    logger.debug(
        f"[PERMS] supervisor_huids загружены из БД: {len(huids_str)} записей"
    )
    return set(huids_str)


async def invalidate_supervisor_cache() -> None:
    """Сбросить кэш супервизоров."""
    await cache_delete(KEY_SUPERVISOR)
    logger.info("[PERMS] Кэш supervisor_huids инвалидирован")


# ══════════════════════════════════════════════════════════════
# Утилита для инвалидации обоих кэшей
# ══════════════════════════════════════════════════════════════
async def invalidate_all_permissions() -> None:
    """Сбросить оба кэша — например, при полной синхронизации."""
    await invalidate_support_cache()
    await invalidate_supervisor_cache()