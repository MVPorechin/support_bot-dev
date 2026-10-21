"""
Сервис проактивного показа панели смены специалисту.

Вызывается из /status handler при переключении пользователя на чат с ботом.
Показывает кнопку /online или /offline только в подходящий момент.
"""
import logging
import uuid

from pybotx import Bot, BubbleMarkup, KeyboardMarkup, Button

from config import BotConfig
from db.tickets.models import Supervisor
from services.cache_service import cache_set_if_absent
from services.supervisor_service import get_supervisor_service
from services.cache_service import cache_delete

logger = logging.getLogger(__name__)

# ── Кэш: не показывать панель чаще раза в N секунд ─────────
PANEL_CACHE_TTL_SEC = 1800          # 30 минут
PANEL_CACHE_KEY_PREFIX = "cache:spec_panel_shown"


def _panel_cache_key(huid: uuid.UUID) -> str:
    return f"{PANEL_CACHE_KEY_PREFIX}:{huid}"


def _build_shift_keyboard() -> KeyboardMarkup:
    """Клавиатура внизу чата с кнопкой /online или /offline."""
    kb = KeyboardMarkup()

    kb.add_button(command="/sv_spec_list", label="Все специалисты в сети",new_row=False)
    kb.add_button(command="/sv_list", label="Все активные тикеты",)
    kb.add_button(command="/sv_sla", label="SLA всех активных тикетов", new_row=False)
    kb.add_button(command="/sv_queue", label="Очередь и распределение",)
    kb.add_button(command="/sv_start", label="START", new_row=False)

    return kb

async def maybe_show_shift_panel(
    bot: Bot,
    user_huid: uuid.UUID,
) -> None:
    """

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
    supervisor_service = get_supervisor_service()
    supervisor = await supervisor_service.get_by_id(supervisor_id=user_huid)

    if supervisor is None:
        return

    # ── 3. Формируем клавиатуру по статусу ──────────────────
    keyboard = _build_shift_keyboard()

    # ── 4. Отправляем ───────────────────────────────────────
    try:
        await bot.send_message(
            bot_id=uuid.UUID(BotConfig.SUPERVISOR_BOT_ID),   # бот supervisor
            chat_id=supervisor.chat_id,
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