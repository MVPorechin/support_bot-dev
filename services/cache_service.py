"""
Redis-клиент и утилиты кэширования.

Используется во всех фоновых задачах (RedisLock) и в коллекторах
(кэш HUID'ов поддержки и супервизоров).
"""
"""Redis-клиент и утилиты кэширования."""
import json
import uuid
import logging
from typing import Optional, Any
from redis.asyncio import Redis
from config import RedisConfig
from config import get_redis_config


logger = logging.getLogger(__name__)

_redis = Redis.from_url(
    get_redis_config().URL,
)


def get_redis() -> Redis:
    """Ленивый singleton Redis-клиента."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(
            RedisConfig().redis_url,
            encoding="utf-8",
            decode_responses=True,
            max_connections=20,
        )
    return _redis

async def get_cached_chat_id(
    supervisor_huid: uuid.UUID,
) -> Optional[uuid.UUID]:
    """Возвращает chat_id из кэша или None."""
    raw = await cache_get(_cache_key(supervisor_huid))
    if raw is None:
        return None

    # ── Нормализация: bytes → str ───────────────────────────
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            logger.warning(
                f"[SV_CACHE] Не удалось декодировать chat_id "
                f"для {supervisor_huid}: {raw!r}"
            )
            await cache_delete(_cache_key(supervisor_huid))
            return None

    # ── Пустая строка → None ────────────────────────────────
    raw = raw.strip() if isinstance(raw, str) else raw
    if not raw:
        await cache_delete(_cache_key(supervisor_huid))
        return None

    # ── Парсинг UUID ────────────────────────────────────────
    try:
        return uuid.UUID(raw)
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            f"[SV_CACHE] Битый chat_id в кэше для "
            f"{supervisor_huid}: {raw!r}"
        )
        await cache_delete(_cache_key(supervisor_huid))
        return None

async def cache_get(key: str) -> Optional[str]:
    try:
        return await get_redis().get(key)
    except Exception as exc:
        logger.error(f"[CACHE] Ошибка GET {key}: {exc}")
        return None


async def cache_set(key: str, value: str, ttl: int) -> bool:
    try:
        await get_redis().set(key, value, ex=ttl)
        return True
    except Exception as exc:
        logger.error(f"[CACHE] Ошибка SET {key}: {exc}")
        return False


async def cache_set_if_absent(key: str, value: str, ttl: int) -> bool:
    """
    SET NX EX — устанавливает значение, только если ключа нет.
    Возвращает True, если установлено (ключ не существовал).
    """
    try:
        return bool(
            await get_redis().set(key, value, ex=ttl, nx=True)
        )
    except Exception as exc:
        logger.error(f"[CACHE] Ошибка SETNX {key}: {exc}")
        return False


async def cache_exists(key: str) -> bool:
    try:
        return (await get_redis().exists(key)) > 0
    except Exception as exc:
        logger.error(f"[CACHE] Ошибка EXISTS {key}: {exc}")
        return False


async def cache_delete(key: str) -> None:
    try:
        await get_redis().delete(key)
    except Exception as exc:
        logger.error(f"[CACHE] Ошибка DELETE {key}: {exc}")


async def cache_json_set(key: str, obj: Any, ttl: int) -> bool:
    return await cache_set(key, json.dumps(obj, ensure_ascii=False), ttl)


async def cache_json_get(key: str) -> Optional[Any]:
    raw = await cache_get(key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(f"[CACHE] Битый JSON в {key}")
        return None


class SupportCache:
    """Кэш списков HUID'ов с TTL 60 секунд."""

    KEY_SUPPORT = "cache:support:huids"
    KEY_SUPERVISOR = "cache:supervisor:huids"
    TTL = 60

    async def get_huids(self, kind: str) -> set[str]:
        key = self.KEY_SUPPORT if kind == "support" else self.KEY_SUPERVISOR
        data = await get_redis().get(key)
        return set(json.loads(data)) if data else set()

    async def set_huids(self, kind: str, huids: list[str]) -> None:
        key = self.KEY_SUPPORT if kind == "support" else self.KEY_SUPERVISOR
        await get_redis().set(key, json.dumps(huids), ex=self.TTL)

    async def invalidate(self, kind: str) -> None:
        key = self.KEY_SUPPORT if kind == "support" else self.KEY_SUPERVISOR
        await get_redis().delete(key)