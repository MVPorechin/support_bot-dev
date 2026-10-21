"""
Кэш chat_id супервизоров.

Хранит в Redis: cache:sv_chat_id:<supervisor_huid> → chat_id (UUID string).
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
from services.supervisor_service import SupervisorService

logger = logging.getLogger(__name__)

CACHE_KEY_PREFIX = "cache:sv_chat_id"
LOCK_KEY_PREFIX = "lock:sv_chat_id_load"
CACHE_TTL_SEC = 300          # 5 минут
LOAD_LOCK_TTL_SEC = 10       # защита от «шторма» при промахе кэша


def _cache_key(huid: uuid.UUID) -> str:
    return f"{CACHE_KEY_PREFIX}:{huid}"


def _lock_key(huid: uuid.UUID) -> str:
    return f"{LOCK_KEY_PREFIX}:{huid}"


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


async def set_cached_chat_id(
    supervisor_huid: uuid.UUID,
    chat_id: uuid.UUID,
) -> None:
    """Кладёт chat_id в кэш с TTL."""
    await cache_set(
        _cache_key(supervisor_huid),
        str(chat_id),
        ttl=CACHE_TTL_SEC,
    )


async def invalidate_cached_chat_id(
    supervisor_huid: uuid.UUID,
) -> None:
    """Сбрасывает кэш для конкретного супервизора."""
    await cache_delete(_cache_key(supervisor_huid))


async def get_or_load_chat_id(
    supervisor_huid: uuid.UUID,
    incoming_chat_id: uuid.UUID,
) -> Optional[uuid.UUID]:
    """
    Главный вход. Логика:
      1. Пробуем взять из кэша — если есть, вернуть.
      2. Если нет — берём lock на загрузку (SET NX), чтобы только один
         воркер пошёл в БД.
      3. Читаем супервизора из БД.
      4. Если у него уже есть chat_id — кладём в кэш и возвращаем.
      5. Если нет — записываем incoming_chat_id в БД, кладём в кэш,
         возвращаем.
      6. Если lock не взят (другой воркер уже грузит) — делаем быструю
         повторную проверку кэша, возможно, значение уже там.
    """
    # ── 1. Кэш ──────────────────────────────────────────────
    cached = await get_cached_chat_id(supervisor_huid)
    if cached is not None:
        return cached

    # ── 2. Lock на загрузку ─────────────────────────────────
    got_lock = await cache_set_if_absent(
        _lock_key(supervisor_huid),
        "1",
        ttl=LOAD_LOCK_TTL_SEC,
    )

    if not got_lock:
        # Другой воркер уже грузит — быстро проверим кэш ещё раз
        # (возможно, значение уже туда положили)
        cached = await get_cached_chat_id(supervisor_huid)
        return cached

    # ── 3-5. Мы владеем локом ───────────────────────────────
    try:
        svc = SupervisorService()
        supervisor = await svc.get_by_id(supervisor_huid)

        # Не супервизор — ничего не делаем
        if supervisor is None:
            return None

        # Уже есть chat_id — просто кэшируем
        if supervisor.chat_id is not None:
            await set_cached_chat_id(supervisor_huid, supervisor.chat_id)
            return supervisor.chat_id

        # Заполняем chat_id в БД
        await svc.ensure_chat_id(
            supervisor_id=supervisor_huid,
            chat_id=incoming_chat_id,
        )
        await set_cached_chat_id(supervisor_huid, incoming_chat_id)
        logger.info(
            f"[SV_CACHE] chat_id заполнен и закэширован для "
            f"{supervisor_huid}: {incoming_chat_id}"
        )
        return incoming_chat_id

    except Exception as exc:
        logger.exception(
            f"[SV_CACHE] Ошибка загрузки chat_id для {supervisor_huid}: {exc}"
        )
        return None