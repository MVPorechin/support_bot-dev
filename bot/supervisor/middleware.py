import logging
import uuid

from pybotx import Bot, IncomingMessage, IncomingMessageHandlerFunc

from services.audit_service import correlation_id_var
from services.supervisor_cache import get_or_load_chat_id
from services.supervisor_context import set_supervisor_context

logger = logging.getLogger(__name__)


async def supervisor_middleware(
    message: IncomingMessage,
    bot: Bot,
    call_next: IncomingMessageHandlerFunc,
) -> None:
    # correlation_id для аудита
    correlation_id_var.set(uuid.uuid4())

    # Игнорируем самого бота
    if message.sender.huid == message.bot.id:
        await call_next(message, bot)
        return

    # ── Bulk upload CSV ─────────────────────────────────────
    if message.file:
        from bot.supervisor.collector import try_handle_supervisor_upload
        if await try_handle_supervisor_upload(message, bot):
            return

    # ── Определяем chat_id супервизора ──────────────────────
    chat_id = await get_or_load_chat_id(
        supervisor_huid=message.sender.huid,
        incoming_chat_id=message.chat.id,
    )

    # Пробрасываем в контекст — хендлеры смогут прочитать
    set_supervisor_context(
        huid=message.sender.huid,
        chat_id=chat_id,
    )

    await call_next(message, bot)