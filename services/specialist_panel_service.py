"""
Сервис проактивного показа панели смены специалисту.

Вызывается из /status handler при переключении пользователя на чат с ботом.
Показывает кнопку /online или /offline только в подходящий момент.
"""
import logging
import uuid

from pybotx import Bot, BubbleMarkup, KeyboardMarkup, Button

from config import BotConfig
from db.tickets.models import SupportStatus
from services.cache_service import cache_set_if_absent
from services.support_service import SupportService
from services.cache_service import cache_delete

logger = logging.getLogger(__name__)

# ── Кэш: не показывать панель чаще раза в N секунд ─────────
PANEL_CACHE_TTL_SEC = 1800          # 30 минут
PANEL_CACHE_KEY_PREFIX = "cache:spec_panel_shown"


def _panel_cache_key(huid: uuid.UUID) -> str:
    return f"{PANEL_CACHE_KEY_PREFIX}:{huid}"


def _build_shift_keyboard(status: SupportStatus) -> KeyboardMarkup:
    """Клавиатура внизу чата с кнопкой /online или /offline."""
    kb = KeyboardMarkup()

    if status == SupportStatus.ONLINE:
        kb.add_button(command="/offline", label="🔴 Завершить смену")
    elif status == SupportStatus.OFFLINE:
        kb.add_button(command="/online", label="🟢 Начать смену")

    return kb

async def maybe_show_shift_panel(
    bot: Bot,
    user_huid: uuid.UUID,
) -> None:
    """
    Если пользователь — специалист поддержки, показывает ему панель смены
    (кнопка /online или /offline).

    Идемпотентно и защищено кэшем: повторные вызовы в течение
    PANEL_CACHE_TTL_SEC ничего не делают.
    """
    # ── 1. Антидубль через Redis ────────────────────────────
    cache_key = _panel_cache_key(user_huid)
    was_absent = await cache_set_if_absent(
        cache_key,
        "1",
        ttl=PANEL_CACHE_TTL_SEC,
    )
    if not was_absent:
        # Уже показывали меньше 30 минут назад
        return

    # ── 2. Проверяем, что это специалист ────────────────────
    support_service = SupportService()
    specialist = await support_service.get_support_by_id(user_huid)

    if specialist is None:
        return

    # ── 3. Формируем клавиатуру по статусу ──────────────────
    keyboard = _build_shift_keyboard(specialist.status)

    # ── 4. Отправляем ───────────────────────────────────────
    try:
        await bot.send_message(
            bot_id=uuid.UUID(BotConfig.BOT_ID),   # бот поддержки
            chat_id=specialist.chat_id,
            recipients=[user_huid],
            body='',
            keyboard=keyboard,
            wait_callback=False,
            send_push=False
        )

    except Exception as exc:
        logger.exception(
            f"[PANEL] Не удалось показать панель специалисту {user_huid}: {exc}"
        )
        # Сбрасываем кэш, чтобы при следующем переключении попробовать снова

        await cache_delete(cache_key)