import logging
from typing import Optional
from uuid import UUID, uuid4

from pybotx import Bot, IncomingMessage, IncomingMessageHandlerFunc

from config import BotConfig
from services.ticket_service import TicketService, TicketStatus, get_support_service
from services.audit_service import correlation_id_var
from services.support_cache import set_cached_chat_id
from bot.supervisor.collector import try_handle_supervisor_upload

logger = logging.getLogger(__name__)

# --- Dependency Injection / Lazy Singletons ---
# Предотвращает спам инстанцированиями и подключениями к БД на каждое сообщение
_ticket_service: Optional[TicketService] = None


def get_ticket_service() -> TicketService:
    global _ticket_service
    if _ticket_service is None:
        _ticket_service = TicketService()
    return _ticket_service


def _get_display_name(username: Optional[str]) -> str:
    """
    Безопасно извлекает фамилию (или имя) для приветствия.
    Защита от IndexError и AttributeError.
    """
    if not username:
        return "Коллега"
    parts = username.split()
    # Берем фамилию, если есть, иначе имя, иначе заглушку
    if len(parts) > 1:
        return parts[1]
    return parts[0] if parts else "Коллега"


async def ticket_creation_middleware(
        message: IncomingMessage,
        bot: Bot,
        call_next: IncomingMessageHandlerFunc,
) -> None:
    correlation_id_var.set(uuid4())
    try:
        # 1. Защита от циклов (игнорируем самого бота)
        if message.sender.huid == message.bot.id:
            await call_next(message, bot)
            return

        # ── [НОВОЕ] Ветка 0: файл от супервизора ────────────
        if message.file:
            if await try_handle_supervisor_upload(message, bot):
                return

        # 2. Защита от None (стикеры, картинки без текста)
        body = message.body or ""
        is_command = body.startswith("/")

        # 3. Команды и общий чат поддержки делегируются профильным хендлерам
        support_chat_id = UUID(BotConfig.SUPPORT_CHAT_ID)
        if is_command or message.chat.id == support_chat_id:
            await call_next(message, bot)
            return

        ticket_service = get_ticket_service()
        message_body = body if body else "[Медиа-вложение]"

        # === Ветка 1: Сообщение от специалиста в треде ===
        # Ищем тикет по support_thread_message_id (ID треда совпадает с ID чата в eXpress)
        ticket = await ticket_service.repo.get_ticket_by_thread_id(message_id=message.chat.id)

        if ticket:
            logger.info(f"[MIDDLEWARE] Сообщение от специалиста в треде тикета #{ticket.ticket_id}")

            # Пересылаем ответ пользователю в личку
            await ticket_service.forward_reply_to_user(message, bot, ticket)

            # Сохраняем в историю (репозиторий сам безопасно склеит текст)
            await ticket_service.create_or_update_ticket(
                message=message,
                bot=bot,
                user_huid=ticket.user_huid,
                message_body=message_body,
                user_name=ticket.user_name,
                chat_id=ticket.chat_id,
                role='support'
            )
            # фиксируем что оператор ответил пользователю
            await ticket_service.mark_operator_reply(ticket_id=ticket.ticket_id)
            await call_next(message, bot)
            return

        # === Ветка 2: Сообщение из личного чата пользователя ===
        active_ticket = await ticket_service.repo.get_open_ticket_by_user(message.sender.huid)

        if active_ticket:
            logger.info(
                f"[MIDDLEWARE] Сообщение от пользователя, тикет #{active_ticket.ticket_id}, "
                f"статус: {active_ticket.status}"
            )

            if active_ticket.status == TicketStatus.IN_WORK:
                # Тикет в работе — пересылаем специалисту в тред
                await ticket_service.forward_message_to_support(
                    message,
                    bot,
                    ticket_id=active_ticket.ticket_id,
                    support_chat_id=active_ticket.support_thread_message_id
                )
                await ticket_service.create_or_update_ticket(
                    message=message,
                    bot=bot,
                    user_huid=active_ticket.user_huid,
                    message_body=message_body,
                    user_name=active_ticket.user_name,
                    chat_id=active_ticket.chat_id,
                    role='user'
                )
                # фиксируем что пользователь ответил оператору
                await ticket_service.mark_user_reply(ticket_id=active_ticket.ticket_id)
                await call_next(message, bot)
                return

            elif active_ticket.status in (TicketStatus.NEW, TicketStatus.OPEN):
                # Тикет в очереди — дописываем в БД, не шлём в чат поддержки
                await ticket_service.create_or_update_ticket(
                    message=message,
                    bot=bot,
                    user_huid=active_ticket.user_huid,
                    message_body=message_body,
                    user_name=active_ticket.user_name,
                    chat_id=active_ticket.chat_id,
                    role='user'
                    )
                await call_next(message, bot)
                return

            elif active_ticket.status == TicketStatus.SOLVED and active_ticket.comment is None:
                # Ждем комментарий от пользователя
                await ticket_service.save_comment(
                    ticket_id=active_ticket.ticket_id,
                    comment=message_body,
                )
                await bot.answer_message("Спасибо за ваш комментарий!",wait_callback=False)
                await ticket_service.request_additional_quest(bot, ticket=active_ticket)
                await call_next(message, bot)
                return

            elif active_ticket.status == TicketStatus.SOLVED and active_ticket.comment is not None:
                # ── НОВОЕ: Пользователь пишет после того, как уже оставил комментарий/оценку.
                # Закрываем старый тикет и создаем новый.
                logger.info(
                    f"[MIDDLEWARE] Тикет #{active_ticket.ticket_id} в SOLVED с комментарием. "
                    "Закрываем его и создаем новый."
                )

                # 1. Принудительно закрываем старый тикет, чтобы create_or_update не дописал в него
                await ticket_service.repo.close_ticket(active_ticket.ticket_id)

                # 2. Создаем новый тикет
                new_ticket = await ticket_service.create_or_update_ticket(
                    message=message,
                    user_huid=message.sender.huid,
                    bot=bot,
                    message_body=message_body,
                    user_name=message.sender.username,
                    chat_id=message.chat.id,
                    role='user'
                    )

                # ── Сразу создаем тред поддержки и отправляем туда первое сообщение (включая файлы) ──
                support_chat_id = await ticket_service.get_support_chat_by_user(user_huid=message.sender.huid)
                await ticket_service.repo.set_support_chat_id(new_ticket.ticket_id, support_chat_id)
                # Пересылаем первое сообщение в тред (файлы не потеряются)
                await ticket_service.forward_message_to_support(
                    message,
                    bot,
                    ticket_id=new_ticket.ticket_id,
                    support_chat_id=support_chat_id,
                    show_buttons=False
                )
                # --------------------------------------------------------------------------------

                greeting_name = _get_display_name(message.sender.username)
                await bot.answer_message(
                    f"{greeting_name}, здравствуйте!",
                    wait_callback=False
                )

                autoassign = await ticket_service.auto_assign_ticket(new_ticket.ticket_id, bot)
                if not autoassign:
                    await bot.answer_message(
                        "Спасибо, что написали нам. Сейчас все операторы заняты.\n"
                        "Как только специалист освободится, он сразу подключится к чату и ответит на Ваш вопрос. "
                        "Пожалуйста, ожидайте.",
                        wait_callback=False
                    )
                await call_next(message, bot)
                return

            elif active_ticket.status == TicketStatus.PENDING:
                logger.info(
                    f"[MIDDLEWARE] Пользователь ответил в тикете #{active_ticket.ticket_id}, "
                    "который находится в статусе PENDING"
                )

                await ticket_service.create_or_update_ticket(
                    message=message,
                    bot=bot,
                    user_huid=active_ticket.user_huid,
                    message_body=message_body,
                    user_name=active_ticket.user_name,
                    chat_id=active_ticket.chat_id,
                    role='user'
                    )

                await ticket_service.reactivate_pending_ticket(
                    ticket_id=active_ticket.ticket_id,
                    bot=bot,
                )

                await call_next(message, bot)
                return

            else:
                # ── Страховка от "зависших" тикетов ──
                # Если тикет в каком-то неожиданном статусе, закрываем его и создаем новый.
                logger.info(
                    f"[MIDDLEWARE] Тикет #{active_ticket.ticket_id} в статусе {active_ticket.status}. "
                    "Закрываем и создаем новый."
                )

                if active_ticket.status != TicketStatus.CLOSED:
                    await ticket_service.repo.close_ticket(active_ticket.ticket_id)

                new_ticket = await ticket_service.create_or_update_ticket(
                    message=message,
                    user_huid=message.sender.huid,
                    bot=bot,
                    message_body=message_body,
                    user_name=message.sender.username,
                    chat_id=message.chat.id,
                    role='user'
                    )

                # ── Сразу создаем тред поддержки и отправляем туда первое сообщение (включая файлы) ──
                support_chat_id = await ticket_service.get_support_chat_by_user(user_huid=message.sender.huid)
                if support_chat_id is None:
                    support_chat_id = await ticket_service.create_chat_for_support(bot, ticket)
                    logger.info(f'Чат создали, так как его не было: {support_chat_id}')
                await ticket_service.repo.set_support_chat_id(new_ticket.ticket_id, support_chat_id)
                # Пересылаем первое сообщение в тред (файлы не потеряются)
                await ticket_service.forward_message_to_support(
                    message,
                    bot,
                    ticket_id=new_ticket.ticket_id,
                    support_chat_id=support_chat_id,
                    show_buttons=False
                )
                # --------------------------------------------------------------------------------

                greeting_name = _get_display_name(message.sender.username)
                await bot.answer_message(f"{greeting_name}, здравствуйте!",wait_callback=False)
                autoassign = await ticket_service.auto_assign_ticket(new_ticket.ticket_id, bot)
                if not autoassign:
                    await bot.answer_message(
                        "Спасибо, что написали нам. Сейчас все операторы заняты.\n"
                        "Как только специалист освободится, он сразу подключится к чату и ответит на Ваш вопрос. "
                        "Пожалуйста, ожидайте.",
                        wait_callback=False
                    )
                await call_next(message, bot)
                return

        else:
            # Тикета нет — создаём новый
            logger.info(f"[MIDDLEWARE] Создаём новое обращение для пользователя {message.sender.huid}")
            new_ticket = await ticket_service.create_or_update_ticket(
                message=message,
                user_huid=message.sender.huid,
                bot=bot,
                message_body=message_body,
                user_name=message.sender.username,
                chat_id=message.chat.id,
                role='user'
            )

            # ── Сразу создаем тред поддержки и отправляем туда первое сообщение (включая файлы) ──
            support_chat_id = await ticket_service.get_support_chat_by_user(user_huid=message.sender.huid)
            if support_chat_id is None:
                support_chat_id = await ticket_service.create_chat_for_support(bot, new_ticket)
                logger.info(f'Чат создали, так как его не было: {support_chat_id}')
            await ticket_service.repo.set_support_chat_id(new_ticket.ticket_id, support_chat_id)
            # Пересылаем первое сообщение в тред (файлы не потеряются)
            await ticket_service.forward_message_to_support(
                message,
                bot,
                ticket_id=new_ticket.ticket_id,
                support_chat_id=support_chat_id,
                show_buttons=False
            )
            # --------------------------------------------------------------------------------

            # Безопасное извлечение имени
            greeting_name = _get_display_name(message.sender.username)

            await bot.answer_message(f"{greeting_name}, здравствуйте!")
            autoassign = await ticket_service.auto_assign_ticket(new_ticket.ticket_id, bot)
            if not autoassign:
                await bot.answer_message(
                    "Спасибо, что написали нам. Сейчас все операторы заняты.\n"
                    "Как только специалист освободится, он сразу подключится к чату и ответит на Ваш вопрос. "
                    "Пожалуйста, ожидайте.",
                wait_callback=False
                )

        await call_next(message, bot)

    except Exception as exc:
        logger.exception(f"[MIDDLEWARE] Критическая ошибка при обработке сообщения: {exc}")

        await call_next(message, bot)

async def _maybe_fill_specialist_chat_id(
        message: IncomingMessage,
        bot:Bot,
        call_next:IncomingMessageHandlerFunc
) -> None:
    """
    Если отправитель — специалист поддержки и у него нет chat_id,
    заполняет его текущим chat_id из сообщения.
    """
    try:
        svc = get_support_service()
        specialist = await svc.repo.get_by_id(message.sender.huid)

        if specialist is None:
            return   # обычный пользователь

        if specialist.chat_id is not None:
            return   # уже заполнено

        await svc.ensure_chat_id(
            support_id=message.sender.huid,
            chat_id=message.chat.id,
        )

        # Кэш
        await set_cached_chat_id(
            message.sender.huid,
            message.chat.id,
        )

    except Exception as exc:
        # Middleware не должен падать из-за этого
        logger.exception(
            f"[MIDDLEWARE] Ошибка автозаполнения chat_id "
            f"для {message.sender.huid}: {exc}"
        )
    finally:
        await call_next(message, bot)