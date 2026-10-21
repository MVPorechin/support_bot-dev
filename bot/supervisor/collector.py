# bot/collector.py
import asyncio
import datetime
import logging
import os
import time
import uuid
from zoneinfo import ZoneInfo
from datetime import datetime, timedelta, timezone
from sqlalchemy import select, desc
import aiofiles
from pybotx import (
    Bot,
    BubbleMarkup,
    HandlerCollector,
    IncomingMessage,
    MentionBuilder,
    StatusRecipient, 
    UserKinds, 
    OutgoingAttachment,
    BubbleMarkup
)

from config import BotConfig, RegistrationConfig
from db.tickets.models import TicketStatus, SupportStatus, WorkSessionEndReason
from services.shift_service import ShiftService
from services.ticket_service import TicketService
from services.support_service import SupportService
from services.supervisor_service import SupervisorService
from services.ticket_transfer_service import TicketTransferService
from services.export_service import ExportService
from services.supervisor_service import get_supervisor_service
from services.supervisor_cache import (
    set_cached_chat_id,
    invalidate_cached_chat_id,
    cache_delete
)
from services.supervisor_context import set_supervisor_context
from services.permissions_cache import get_supervisor_huids_cached, invalidate_support_cache
from services.audit_service import get_audit_service
from db.audit.models import AuditAction, AuditSeverity
logger = logging.getLogger(__name__)

supervisor_collector = HandlerCollector()
# _supervisor_chat_id = uuid.UUID(BotConfig.SUPERVISOR_CHAT_ID)
MSK = ZoneInfo("Europe/Moscow")
DEFAULT_EXPORT_LIMIT = 20
MAX_EXPORT_LIMIT = 1000
AUDIT_ACTOR_DEFAULT_LIMIT = 100
AUDIT_ACTOR_MAX_LIMIT = 500

# ── Lazy Singletons ──────────────────────────────────────────
_ticket_service: TicketService | None = None
_support_service: SupportService | None = None
_supervisor_service: SupervisorService | None = None
_transfer_service: TicketTransferService | None = None
_export_service: ExportService | None = None
_shift_service : ShiftService | None = None

def get_export_service() -> ExportService:
    global _export_service
    if _export_service is None:
        _export_service = ExportService()
    return _export_service

def get_ticket_service() -> TicketService:
    global _ticket_service
    if _ticket_service is None:
        _ticket_service = TicketService()
    return _ticket_service


def get_support_service() -> SupportService:
    global _support_service
    if _support_service is None:
        _support_service = SupportService()
    return _support_service


def get_transfer_service() -> TicketTransferService:
    global _transfer_service
    if _transfer_service is None:
        _transfer_service = TicketTransferService()
    return _transfer_service

def get_shift_service():
    global _shift_service
    if _shift_service is None:
        _shift_service = ShiftService()
    return _shift_service

# Глобальное состояние «ждём файл»
_awaiting_upload: dict[uuid.UUID, float] = {}

def _parse_name_and_limit(args: list[str]) -> tuple[str, int] | tuple[None, None]:
    """
    Извлекает ФИО и limit из аргументов команды.
    Формат: ['/sv_export_support', 'Иванов', 'Иван', '50'] → ('Иванов Иван', 50)
    """
    body = args[1:]
    if not body:
        return None, None

    limit = DEFAULT_EXPORT_LIMIT
    if body[-1].isdigit():
        limit = max(1, min(int(body[-1]), MAX_EXPORT_LIMIT))
        body = body[:-1]

    if not body:
        return None, None

    return " ".join(body), limit

async def _is_registered_supervisor(huid: uuid.UUID) -> bool:
    """Проверяет, есть ли huid в таблице supervisors и активен ли он."""
    svc = get_supervisor_service()
    sup = await svc.get_by_id(huid)
    return sup is not None and sup.is_active

async def _send_csv_to_chat(
    bot: Bot,
    message: IncomingMessage,
    file_path: str,
    filename: str,
    header: str,
) -> int:
    """Общая отправка CSV. Возвращает размер файла."""
    try:
        # Читаем файл и создаем OutgoingAttachment согласно документации pybotx

        async with aiofiles.open(file_path, "rb") as f:
            content = await f.read()
            await f.seek(0)
            attachment = await OutgoingAttachment.from_async_buffer(f, filename)

        size = len(content)

        await bot.answer_message(
            f"{header}\n"
            f"📊 Файл: {filename}\n"
            f"💾 Размер: {size} байт",
            file=attachment,wait_callback=False
        )
        return size
    finally:
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception as e:
                logger.warning(f"Не удалось удалить {file_path}: {e}")


# в sv_start при регистрации по инвайту
async def _invalidate_chat_check(huid: uuid.UUID) -> None:
    await cache_delete(f"mw:sv_chat_check:{huid}")

async def is_supervisor(
    status_recipient: StatusRecipient,
    bot: Bot,
) -> bool:
    """Проверяет, является ли пользователь активным супервизором."""
    huids = await get_supervisor_huids_cached()
    return str(status_recipient.huid) in huids


# ── Команды супервизора ─────────────────────────────────────
@supervisor_collector.command(
    "/sv_export_period",
    description="Экспорт тикетов за период",
    visible=is_supervisor,
)
async def sv_export_period(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()

    # Минимум: команда + 2 даты
    if len(args) < 3:
        await bot.answer_message(
            "❌ Использование: `/sv_export_period <YYYY-MM-DD> <YYYY-MM-DD> [limit]`\n"
            "Примеры:\n"
            "• `/sv_export_period 2026-09-01 2026-09-14`\n"
            "• `/sv_export_period 2026-09-01 2026-09-14 500`",wait_callback=False
        )
        return

    try:
        date_from = datetime.strptime(args[1], "%Y-%m-%d").replace(
            hour=0, minute=0, second=0, tzinfo=MSK,
        )
        date_to = datetime.strptime(args[2], "%Y-%m-%d").replace(
            hour=23, minute=59, second=59, tzinfo=MSK,
        )
    except ValueError:
        await bot.answer_message("❌ Неверный формат даты. Используйте `YYYY-MM-DD`.",wait_callback=False)
        return

    if date_from > date_to:
        await bot.answer_message("❌ Дата начала позже даты окончания.",wait_callback=False)
        return

    limit = DEFAULT_EXPORT_LIMIT
    if len(args) >= 4 and args[3].isdigit():
        limit = max(1, min(int(args[3]), MAX_EXPORT_LIMIT))

    await bot.answer_message(
        f"⏳ Ищу тикеты за период "
        f"{date_from.strftime('%d.%m.%Y')} – {date_to.strftime('%d.%m.%Y')} "
        f"(лимит: {limit})...",wait_callback=False
    )

    file_path = None
    try:
        export_service = get_export_service()
        file_path, count, (df, dt_) = await export_service.export_tickets_by_period(
            date_from, date_to, limit=limit,
        )

        if count == 0:
            await bot.answer_message(
                f"📭 За период {df.strftime('%d.%m.%Y')} – {dt_.strftime('%d.%m.%Y')} "
                f"тикетов не найдено.",wait_callback=False
            )
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"period_{df.strftime('%Y%m%d')}_{dt_.strftime('%Y%m%d')}_{ts}.csv"

        await _send_csv_to_chat(
            bot, message, file_path, filename,
            header=(
                f"✅ Найдено **{count}** тикетов.\n"
                f"📅 Период: {df.strftime('%d.%m.%Y')} – {dt_.strftime('%d.%m.%Y')}"
            ),
        )

        # [AUDIT] Экспорт за период
        await get_audit_service().supervisor_action(
            AuditAction.SUPERVISOR_EXPORT_TICKETS,
            ticket_id=0,
            supervisor_huid=message.sender.huid,
            supervisor_name=message.sender.username,
            severity=AuditSeverity.WARNING,
            context={
                "filter": "period",
                "date_from": df.isoformat(),
                "date_to": dt_.isoformat(),
                "limit": limit,
                "found": count,
            },
        )

    except Exception as e:
        logger.exception(f"Ошибка экспорта за период: {e}")
        await bot.answer_message("❌ Ошибка экспорта. Попробуйте позже.",wait_callback=False)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

@supervisor_collector.command(
    "/sv_pending",
    description="Список отложенных тикетов (PENDING)",
    visible=is_supervisor,
)
async def sv_pending(message: IncomingMessage, bot: Bot) -> None:
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message("🚫 Только для действующих супервизоров.",wait_callback=False)
        return

    ticket_service = get_ticket_service()
    tickets = await ticket_service.repo.get_pending_tickets(limit=50)

    if not tickets:
        await bot.answer_message("📭 Отложенных тикетов нет.",wait_callback=False)
        return

    await _render_pending_tickets(bot, message, tickets)


async def _render_pending_tickets(
    bot: Bot,
    message: IncomingMessage,
    tickets: list,
) -> None:
    """Рендерит список PENDING-тикетов с кнопками действий."""
    MAX_SHOWN = 15

    now = datetime.now(MSK)
    lines = [f"⏸ **Отложенные тикеты ({len(tickets)}):**\n"]
    bubbles = BubbleMarkup()

    for t in tickets[:MAX_SHOWN]:
        # Сколько тикет уже в PENDING
        since = t.pending_since.astimezone(MSK) if t.pending_since else None
        if since:
            delta = now - since
            hours = int(delta.total_seconds() // 3600)
            since_str = since.strftime("%d.%m %H:%M")
            wait_str = f"{hours}ч" if hours < 24 else f"{hours // 24}д"
        else:
            since_str, wait_str = "—", "—"

        lines.append(
            f"• `#{t.ticket_id}` — {t.user_name or '—'} — "
            f"в PENDING с {since_str} ({wait_str})"
        )

        # Две кнопки на тикет: в очередь / назначить на специалиста
        bubbles.add_button(
            command=f"/sv_reactivate {t.ticket_id}",
            label=f"🔥 #{t.ticket_id} в очередь",
            background_color="#FFE0B2",
        )
        bubbles.add_button(
            command=f"/sv_reactivate_to {t.ticket_id}",
            label=f"👤 #{t.ticket_id} назначить",
            background_color="#C8E6C9",
            new_row=False,
        )

    if len(tickets) > MAX_SHOWN:
        lines.append(f"\n...и ещё {len(tickets) - MAX_SHOWN}")

    lines.append(
        "\n🔥 — вернуть в общую очередь (автоназначение с приоритетом)"
        "\n👤 — выбрать специалиста и назначить напрямую"
    )

    await bot.answer_message("\n".join(lines), bubbles=bubbles,wait_callback=False)

@supervisor_collector.command(
    "/sv_reactivate",
    description="Вернуть PENDING-тикет в общую очередь",
    visible=False,
)
async def sv_reactivate(message: IncomingMessage, bot: Bot) -> None:
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message("🚫 Только для действующих супервизоров.",wait_callback=False)
        return

    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /sv_reactivate <ticket_id>",wait_callback=False)
        return

    try:
        ticket_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID тикета должен быть числом.",wait_callback=False)
        return

    ticket_service = get_ticket_service()

    # Проверяем, что тикет действительно в PENDING
    ticket = await ticket_service.repo.get_ticket_by_id(ticket_id)

    if ticket.status != TicketStatus.PENDING:
        await bot.answer_message(
            f"⚠️ Тикет #{ticket_id} в статусе `{ticket.status.value}`, "
            f"не в PENDING."
        )
        return

    # Используем существующий метод сервиса — он вернёт в очередь
    # с приоритетом и попробует назначить автоматически
    await ticket_service.reactivate_pending_ticket(ticket_id, bot)

    # [AUDIT] Супервизор вернул в очередь вручную
    await get_audit_service().supervisor_action(
        AuditAction.TICKET_REACTIVATED,
        ticket_id=ticket_id,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        context={"source": "supervisor_manual_queue"},
    )

    await bot.answer_message(
        f"✅ Тикет #{ticket_id} возвращён в очередь с приоритетом.\n"
        f"Автоназначение запущено — если есть свободные специалисты, "
        f"тикет будет назначен в течение минуты."
    )

@supervisor_collector.command(
    "/sv_reactivate_to",
    description="Выбрать специалиста для PENDING-тикета",
    visible=False,
)
async def sv_reactivate_to(message: IncomingMessage, bot: Bot) -> None:
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message("🚫 Только для действующих супервизоров.",wait_callback=False)
        return

    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /sv_reactivate_to <ticket_id>",wait_callback=False)
        return

    try:
        ticket_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID тикета должен быть числом.",wait_callback=False)
        return

    ticket_service = get_ticket_service()
    ticket = await ticket_service.repo.get_ticket_by_id(ticket_id)

    if ticket.status != TicketStatus.PENDING:
        await bot.answer_message(
            f"⚠️ Тикет #{ticket_id} в статусе `{ticket.status.value}`, "
            f"не в PENDING.",wait_callback=False
        )
        return

    support_service = get_support_service()
    specialists = await support_service.get_online_specialist()

    if not specialists:
        await bot.answer_message(
            "😔 Нет специалистов в сети.\n"
            "Верните тикет в очередь через `/sv_reactivate "
            f"{ticket_id}` — автоназначение сработает, как только "
            "кто-то выйдет онлайн.",wait_callback=False
        )
        return

    bubbles = BubbleMarkup()
    lines = [
        f"👤 Кому назначить тикет #{ticket_id}?\n"
        f"Пользователь: {ticket.user_name or '—'}\n"
    ]

    for spec in specialists[:20]:
        lines.append(f"• {spec.full_name}")
        bubbles.add_button(
            command=f"/sv_reactivate_do {ticket_id} {spec.support_id}",
            label=f"→ {spec.full_name}",
        )

    await bot.answer_message("\n".join(lines), bubbles=bubbles,wait_callback=False)

@supervisor_collector.command(
    "/sv_reactivate_do",
    description="Назначить PENDING-тикет на специалиста",
    visible=False,
)
async def sv_reactivate_do(message: IncomingMessage, bot: Bot) -> None:
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message("🚫 Только для действующих супервизоров.",wait_callback=False)
        return

    args = message.body.split()
    if len(args) < 3:
        await bot.answer_message(
            "❌ Использование: /sv_reactivate_do <ticket_id> <specialist_huid>",wait_callback=False
        )
        return

    try:
        ticket_id = int(args[1])
        specialist_huid = uuid.UUID(args[2])
    except (ValueError, TypeError):
        await bot.answer_message("❌ Неверный формат ID тикета или HUID.",wait_callback=False)
        return

    ticket_service = get_ticket_service()

    ok = await ticket_service.reactivate_pending_and_assign(
        ticket_id=ticket_id,
        specialist_huid=specialist_huid,
        bot=bot,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
    )

    if not ok:
        await bot.answer_message(
            f"⚠️ Не удалось назначить тикет #{ticket_id}.\n"
            f"Возможно, он уже вернулся в работу или был закрыт.",wait_callback=False
        )
        return

    support_service = get_support_service()
    spec = await support_service.get_support_by_id(specialist_huid)
    spec_name = spec.full_name if spec else str(specialist_huid)[:8]

    await bot.answer_message(
        f"✅ Тикет #{ticket_id} возвращён в работу и назначен на {spec_name}.",wait_callback=False
    )

@supervisor_collector.command(
    "/sv_export_user",
    description="Экспорт тикетов по пользователю",
    visible=is_supervisor,
)
async def sv_export_user(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    name_query, limit = _parse_name_and_limit(args)

    if not name_query:
        await bot.answer_message(
            "❌ Использование: `/sv_export_user <ФИО или Фамилия> [limit]`\n"
            "Примеры:\n"
            "• `/sv_export_user Поречин`\n"
            "• `/sv_export_user Поречин Максим`\n"
            "• `/sv_export_user Поречин 50`",wait_callback=False
        )
        return

    await bot.answer_message(f"⏳ Ищу тикеты пользователя «{name_query}» (лимит: {limit})...",wait_callback=False)

    file_path = None
    try:
        export_service = get_export_service()
        file_path, count, matched = await export_service.export_tickets_by_user(
            name_query, limit=limit,
        )

        if count == 0:
            await bot.answer_message(f"📭 Тикеты по «{name_query}» не найдены.",wait_callback=False)
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"user_{name_query.replace(' ', '_')}_{ts}.csv"

        matched_str = ", ".join(matched[:5])
        if len(matched) > 5:
            matched_str += f" и ещё {len(matched) - 5}"

        await _send_csv_to_chat(
            bot, message, file_path, filename,
            header=(
                f"✅ Найдено **{count}** тикетов.\n"
                f"👤 Пользователи: {matched_str}"
            ),
        )

        # [AUDIT] Экспорт по пользователю — WARNING (персональные данные)
        await get_audit_service().supervisor_action(
            AuditAction.SUPERVISOR_EXPORT_TICKETS,
            ticket_id=0,
            supervisor_huid=message.sender.huid,
            supervisor_name=message.sender.username,
            severity=AuditSeverity.WARNING,
            context={
                "filter": "user_name",
                "query": name_query,
                "limit": limit,
                "found": count,
                "matched_names": matched,
            },
        )

    except Exception as e:
        logger.exception(f"Ошибка экспорта по пользователю: {e}")
        await bot.answer_message("❌ Ошибка экспорта. Попробуйте позже.",wait_callback=False)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


@supervisor_collector.command(
    "/sv_export_support",
    description="Экспорт тикетов по специалисту",
    visible=is_supervisor,
)
async def sv_export_support(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    name_query, limit = _parse_name_and_limit(args)

    if not name_query:
        await bot.answer_message(
            "❌ Использование: `/sv_export_support <ФИО или Фамилия> [limit]`\n"
            "Примеры:\n"
            "• `/sv_export_support Иванов`\n"
            "• `/sv_export_support Иванов Иван`\n"
            "• `/sv_export_support Иванов 100`",wait_callback=False
        )
        return

    await bot.answer_message(f"⏳ Ищу тикеты специалиста «{name_query}» (лимит: {limit})...",wait_callback=False)

    file_path = None
    try:
        export_service = get_export_service()
        file_path, count, matched = await export_service.export_tickets_by_support(
            name_query, limit=limit,
        )

        if count == 0:
            await bot.answer_message(f"📭 Тикеты по «{name_query}» не найдены.",wait_callback=False)
            return

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        filename = f"support_{name_query.replace(' ', '_')}_{ts}.csv"

        matched_str = ", ".join(matched[:5])
        if len(matched) > 5:
            matched_str += f" и ещё {len(matched) - 5}"

        await _send_csv_to_chat(
            bot, message, file_path, filename,
            header=(
                f"✅ Найдено **{count}** тикетов.\n"
                f"👥 Специалисты: {matched_str}"
            ),
        )

        # [AUDIT] Экспорт по специалисту — WARNING
        await get_audit_service().supervisor_action(
            AuditAction.SUPERVISOR_EXPORT_TICKETS,
            ticket_id=0,
            supervisor_huid=message.sender.huid,
            supervisor_name=message.sender.username,
            severity=AuditSeverity.WARNING,
            context={
                "filter": "support_name",
                "query": name_query,
                "limit": limit,
                "found": count,
                "matched_names": matched,
            },
        )

    except Exception as e:
        logger.exception(f"Ошибка экспорта по специалисту: {e}")
        await bot.answer_message("❌ Ошибка экспорта. Попробуйте позже.",wait_callback=False)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass

@supervisor_collector.command(
    "/sv_export",
    description="Экспорт всех тикетов в CSV",
    visible=is_supervisor
)
async def sv_export(message: IncomingMessage, bot: Bot) -> None:
    """Экспортирует все тикеты из БД в CSV файл и отправляет в чат."""
    await bot.answer_message("⏳ Начинаю экспорт данных... Это может занять несколько секунд.",wait_callback=False)

    file_path = None
    try:
        export_service = get_export_service()
        file_path = await export_service.export_tickets_to_csv()

        # Формируем имя файла с датой

        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f"tickets_export_{timestamp}.csv"

        # Читаем файл и создаем OutgoingAttachment согласно документации pybotx
        async with aiofiles.open(file_path, "rb") as async_file:
            file_content = await async_file.read()
            await async_file.seek(0)

            # Создаем attachment
            file = await OutgoingAttachment.from_async_buffer(async_file, filename)

        # Получаем размер файла для информирования
        file_size = len(file_content)

        # Отправляем файл в чат
        await bot.answer_message(
            f"✅ Экспорт завершен!\n📊 Файл: {filename}\n💾 Размер: {file_size} байт",
            file=file,wait_callback=False
        )

        # [AUDIT] Экспорт — всегда WARNING: это выгрузка чувствительных данных
        await get_audit_service().supervisor_action(
            AuditAction.SUPERVISOR_EXPORT_TICKETS,
            ticket_id=0,  # не привязано к тикету
            supervisor_huid=message.sender.huid,
            supervisor_name=message.sender.username,
            severity=AuditSeverity.WARNING,
            context={"filename": filename, "size_bytes": file_size},
        )

        logger.info(f"Супервизор {message.sender.username} экспортировал данные в CSV")

    except Exception as e:
        logger.exception(f"Ошибка при экспорте данных: {e}")
        await bot.answer_message(
            "❌ Произошла ошибка при экспорте данных. Пожалуйста, попробуйте позже или обратитесь к администратору.",
            wait_callback=False
        )
    finally:
        # Удаляем временный файл
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug(f"Временный файл удален: {file_path}")
            except Exception as cleanup_error:
                logger.warning(f"Не удалось удалить временный файл {file_path}: {cleanup_error}")


@supervisor_collector.command(
    "/sv_export_messages",
    description="Экспорт переписки в CSV",
    visible=is_supervisor
)
async def sv_export_messages(message: IncomingMessage, bot: Bot) -> None:
    """Экспортирует всю переписку из тикетов в CSV файл и отправляет в чат."""
    await bot.answer_message("⏳ Начинаю экспорт переписки... Это может занять несколько секунд.",wait_callback=False)

    file_path = None
    try:
        export_service = get_export_service()
        file_path = await export_service.export_messages_to_csv()

        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f"messages_export_{timestamp}.csv"

        async with aiofiles.open(file_path, "rb") as async_file:
            file_content = await async_file.read()
            await async_file.seek(0)
            file = await OutgoingAttachment.from_async_buffer(async_file, filename)

        file_size = len(file_content)

        # Отправляем файл в чат
        await bot.answer_message(
            f"✅ Экспорт переписки завершен!\n📊 Файл: {filename}\n💾 Размер: {file_size} байт\n💬 Содержит полную историю диалогов",
            file=file,wait_callback=False
        )

        # [AUDIT] Экспорт переписки — WARNING
        await get_audit_service().log(
            action=AuditAction.SUPERVISOR_EXPORT_TICKETS,
            actor_type="supervisor",
            entity_type="export",
            entity_id=filename,
            actor_huid=message.sender.huid,
            actor_name=message.sender.username,
            severity=AuditSeverity.WARNING,
            context={"size_bytes": file_size},
        )

        logger.info(f"Супервизор {message.sender.username} экспортировал переписку в CSV")

    except Exception as e:
        logger.exception(f"Ошибка при экспорте переписки: {e}")
        await bot.answer_message(
            "❌ Произошла ошибка при экспорте переписки. Пожалуйста, попробуйте позже или обратитесь к администратору.",wait_callback=False
        )
    finally:
        # Удаляем временный файл
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
                logger.debug(f"Временный файл удален: {file_path}")
            except Exception as cleanup_error:
                logger.warning(f"Не удалось удалить временный файл {file_path}: {cleanup_error}")


@supervisor_collector.command(
    "/sv_start",
    description="Открыть панель супервизора / принять приглашение",
    visible=False,
)
async def sv_start(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()

    # ══════════════════════════════════════════════════════════
    # 1. Регистрация по коду приглашения
    # ══════════════════════════════════════════════════════════
    if len(args) >= 2:
        try:
            code = uuid.UUID(args[1])
        except (ValueError, TypeError):
            await bot.answer_message("❌ Неверный формат кода приглашения.",wait_callback=False)
            return

        svc = get_supervisor_service()
        success, msg, supervisor = await svc.register_by_invite(
            code=code,
            candidate_huid=message.sender.huid,
            candidate_name=message.sender.username,
        )

        # [AUDIT]
        await get_audit_service().log(
            action=(
                AuditAction.SUPERVISOR_INVITE_USED if success
                else AuditAction.SUPERVISOR_INVITE_FAILED
            ),
            actor_type="supervisor" if success else "user",
            entity_type="supervisor_invite",
            entity_id=str(code),
            actor_huid=message.sender.huid,
            actor_name=message.sender.username,
            severity=AuditSeverity.INFO if success else AuditSeverity.WARNING,
            context={"result": msg},
        )

        if not success:
            await bot.answer_message(f"❌ {msg}",wait_callback=False)
            return

        # ── 2. Сразу фиксируем chat_id ──────────────────────
        # Не ждём middleware — мы уже знаем текущий chat
        current_chat_id = message.chat.id

        try:
            # 2.1. Записываем в БД (идемпотентно)
            await svc.ensure_chat_id(
                supervisor_id=message.sender.huid,
                chat_id=current_chat_id,
            )

            # 2.2. Кладём в Redis, чтобы middleware не пошёл в БД
            await set_cached_chat_id(
                supervisor_huid=message.sender.huid,
                chat_id=current_chat_id,
            )
            # ── Инвалидируем кэш супервизоров ───────────────────
            await invalidate_support_cache()

            # 2.3. Пробрасываем в contextvars текущего запроса
            # (на случай, если понадобится ниже в этом же хендлере)
            set_supervisor_context(
                huid=message.sender.huid,
                chat_id=current_chat_id,
            )

            logger.info(
                f"[SV_START] Супервизор {message.sender.huid} "
                f"зарегистрирован, chat_id={current_chat_id}"
            )

        except Exception as exc:
            # Не роняем регистрацию из-за ошибки записи chat_id —
            # middleware подхватит при следующем сообщении
            logger.exception(
                f"[SV_START] Не удалось сразу записать chat_id "
                f"для {message.sender.huid}: {exc}"
            )

        # ── 3. Приветствие ──────────────────────────────────
        await bot.answer_message(
            "✅ Регистрация супервизора прошла успешно!\n\n"
            "Напишите `/sv_start` без аргументов, чтобы открыть панель.",wait_callback=False
        )
        return

    # ══════════════════════════════════════════════════════════
    # 4. Открытие панели (для действующих супервизоров)
    # ══════════════════════════════════════════════════════════
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message(
            "🚫 Вы не зарегистрированы как супервизор.\n"
            "Попросите действующего супервизора выслать вам "
            "код приглашения и отправьте его командой:\n\n"
            "`/sv_start <код>`",wait_callback=False
        )
        return

    # ── 5. Обновляем chat_id на всякий случай ───────────────
    # Если супервизор сменил аккаунт или кэш был сброшен —
    # middleware сделал бы это сам, но мы «на всякий случай»
    # проверим здесь тоже.
    await _ensure_chat_id_on_panel_open(message)

    await _show_supervisor_panel(message, bot)

async def _ensure_chat_id_on_panel_open(
    message: IncomingMessage,
) -> None:
    """
    Быстрая проверка chat_id при открытии панели.
    Если в кэше уже есть — не трогаем БД.
    """
    from services.supervisor_cache import get_cached_chat_id

    huid = message.sender.huid
    cached = await get_cached_chat_id(huid)
    if cached is not None:
        # Кэш горячий — ничего не делаем
        return

    # Кэш пустой — middleware всё равно подгрузит,
    # но мы опередим его, чтобы фоновые задачи могли эскалировать сразу
    try:
        svc = get_supervisor_service()
        supervisor = await svc.get_by_id(huid)
        if supervisor is None:
            return

        if supervisor.chat_id is not None:
            # В БД уже есть — просто кладём в кэш
            await set_cached_chat_id(huid, supervisor.chat_id)
        else:
            # Первый раз открывает панель без регистрации —
            # заполняем chat_id
            await svc.ensure_chat_id(huid, message.chat.id)
            await set_cached_chat_id(huid, message.chat.id)

    except Exception as exc:
        logger.exception(
            f"[SV_START] Ошибка проверки chat_id для {huid}: {exc}"
        )

async def _show_supervisor_panel(message: IncomingMessage, bot: Bot) -> None:
    """
    Открывает панель супервизора.

    Гарантируется, что вызывается только для действующего супервизора —
    проверка происходит в sv_start ДО вызова этой функции.
    """
    # ── Даты для быстрых отчётов ─────────────────────────────
    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    week_ago = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    month_ago = (now - timedelta(days=30)).strftime("%Y-%m-%d")

    # ── Пending-заявки на регистрацию ────────────────────────
    support_service = get_support_service()
    pending = await support_service.get_pending_registrations()
    pending_count = len(pending)

    # ── Клавиатура ───────────────────────────────────────────
    bubbles = _build_supervisor_bubble(
        today=today,
        week_ago=week_ago,
        month_ago=month_ago,
        pending_count=pending_count,
    )

    # ── Текст приветствия ────────────────────────────────────
    welcome = _build_supervisor_welcome(
        username=message.sender.username,
        pending_count=pending_count,
    )

    await bot.answer_message(welcome, bubbles=bubbles,wait_callback=False)

    # ── Если есть pending — сразу показываем список ─────────
    if pending:
        await _render_pending_registrations(bot, message, pending)


def _build_supervisor_bubble(
    today: str,
    week_ago: str,
    month_ago: str,
    pending_count: int,
) -> BubbleMarkup:
    """Собирает клавиатуру панели супервизора."""
    bubbles = BubbleMarkup()

    # ── Оперативные команды ─────────────────────────────────
    bubbles.add_button(command="/sv_queue",         label="#очередь и распределение")
    bubbles.add_button(command="/sv_sla",           label="#SLA всех тикетов")
    bubbles.add_button(command="/sv_list",          label="#все активные тикеты")
    bubbles.add_button(command="/sv_spec_list",     label="#специалисты в сети")
    bubbles.add_button(command="/sv_transfer",      label="#передать тикет")
    bubbles.add_button(command="/sv_request_ticket", label="#закрыть тикет вручную")
    bubbles.add_button(command="/sv_pending",       label="#отложенные (PENDING)")

    # ── Заявки на регистрацию ───────────────────────────────
    reg_label = (
        f"#заявки на регистрацию ({pending_count})"
        if pending_count else "#заявки на регистрацию"
    )
    bubbles.add_button(command="/sv_pending_reg", label=reg_label)

    # ── Экспорт ─────────────────────────────────────────────
    bubbles.add_button(command="/sv_export",          label="#экспорт всех тикетов")
    bubbles.add_button(command="/sv_export_messages", label="#экспорт переписки")

    # ── Быстрые отчёты ──────────────────────────────────────
    bubbles.add_button(
        command=f"/sv_export_period {today} {today}",
        label="#отчёт: сегодня",
    )
    bubbles.add_button(
        command=f"/sv_export_period {week_ago} {today}",
        label="#отчёт: за неделю",
    )
    bubbles.add_button(
        command=f"/sv_export_period {month_ago} {today}",
        label="#отчёт: за месяц",
    )

    # ── Аудит ───────────────────────────────────────────────
    bubbles.add_button(command="/sv_audit_recent 20", label="#последние события аудита")

    # ── Управление супервизорами ────────────────────────────
    bubbles.add_button(command="/sv_invite_create", label="🎫 Создать приглашение")
    bubbles.add_button(command="/sv_invite_list",   label="📜 Мои приглашения")

    # ── Импорт ──────────────────────────────────────────────
    bubbles.add_button(
        command="/sv_upload_specialists",
        label="#загрузить специалистов",
    )

    return bubbles


def         _build_supervisor_welcome(username: str, pending_count: int) -> str:
    """Формирует приветственный текст с полной шпаргалкой команд."""
    welcome = (
        f"👋 Привет, {username}!\n"
        f"─────────────────────────\n"
        f"📋 **Оперативные команды:**\n"
        f"`/sv_queue` — очередь и распределение\n"
        f"`/sv_sla` — SLA всех тикетов\n"
        f"`/sv_list` — все активные тикеты\n"
        f"`/sv_spec_list` — специалисты в сети\n"
        f"`/sv_transfer` — передать тикет\n"
        f"`/sv_join <ID>` — подключиться к треду (инцидент)\n"
        f"`/sv_request_ticket` — закрыть тикет вручную\n"
        f"`/sv_pending` — отложенные тикеты (PENDING)\n"
        f"\n"
        f"✅ **Модерация:**\n"
        f"`/sv_pending_reg` — заявки на регистрацию\n"
        f"`/sv_approve_reg <ID>` — одобрить\n"
        f"`/sv_reject_reg <ID> [причина]` — отклонить\n"
        f"\n"
        f"📥 **Импорт:**\n"
        f"`/sv_upload_specialists` — загрузка специалистов списком (CSV)\n"
        f"\n"
        f"📊 **Экспорт:**\n"
        f"`/sv_export` — все тикеты в CSV\n"
        f"`/sv_export_messages` — переписка в CSV\n"
        f"`/sv_export_support <ФИО> [limit]` — по специалисту\n"
        f"`/sv_export_user <ФИО> [limit]` — по пользователю\n"
        f"`/sv_export_period <YYYY-MM-DD> <YYYY-MM-DD> [limit]` — за период\n"
        f"Лимит по умолчанию: 20, максимум: 1000.\n"
        f"\n"
        f"🔍 **Аудит:**\n"
        f"`/sv_audit <ticket_id>` — история тикета\n"
        f"`/sv_audit_actor <ФИО или HUID>` — действия сотрудника\n"
        f"`/sv_audit_recent [N]` — последние N событий\n"
        f"\n"
        f"🎫 **Управление супервизорами:**\n"
        f"`/sv_invite_create` — создать приглашение\n"
        f"`/sv_invite_list` — мои приглашения"
    )

    if pending_count:
        welcome += (
            f"\n\n🔔 **Активных заявок на регистрацию: {pending_count}** "
            f"— см. ниже"
        )

    return welcome


@supervisor_collector.command(
    "/sv_invite_create",
    description="Создать код приглашения для нового супервизора",
    visible=is_supervisor,
)
async def sv_invite_create(message: IncomingMessage, bot: Bot) -> None:
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message("🚫 Только для действующих супервизоров.",wait_callback=False)
        return

    svc = get_supervisor_service()
    invite = await svc.create_invite(created_by=message.sender.huid)

    # [AUDIT]
    await get_audit_service().supervisor_action(
        AuditAction.SUPERVISOR_INVITE_CREATED,
        ticket_id=0,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        context={
            "code": str(invite.code),
            "expires_at": invite.expires_at.isoformat(),
        },
    )

    expires = invite.expires_at.astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    await bot.answer_message(
        f"🎫 **Приглашение создано**\n\n"
        f"`{invite.code}`\n\n"
        f"⏱ Действует до: **{expires}**\n"
        f"🔒 Одноразовое\n\n"
        f"Передайте этот код кандидату в супервизоры. "
        f"Он должен отправить боту:\n"
        f"`/sv_start {invite.code}`",wait_callback=False
    )


@supervisor_collector.command(
    "/sv_invite_list",
    description="Мои приглашения",
    visible=is_supervisor,
)
async def sv_invite_list(message: IncomingMessage, bot: Bot) -> None:
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message("🚫 Только для действующих супервизоров.",wait_callback=False)
        return

    svc = get_supervisor_service()
    invites = await svc.get_my_invites(message.sender.huid)

    if not invites:
        await bot.answer_message("📭 Приглашений не найдено.",wait_callback=False)
        return

    now = datetime.now(MSK)
    lines = ["📜 **Мои приглашения:**\n"]

    for inv in invites[:20]:
        expires = inv.expires_at.astimezone(MSK)
        if inv.used_at:
            status = f"✅ использовано {inv.used_at.astimezone(MSK).strftime('%d.%m %H:%M')}"
        elif inv.revoked_at:
            status = f"🚫 отозвано {inv.revoked_at.astimezone(MSK).strftime('%d.%m %H:%M')}"
        elif expires <= now:
            status = "⌛ истекло"
        else:
            status = f"🟢 активно до {expires.strftime('%d.%m %H:%M')}"

        lines.append(f"• `{inv.code}` — {status}")

    await bot.answer_message("\n".join(lines),wait_callback=False)

@supervisor_collector.command(
    "/sv_queue",
    description="Очередь и распределение",
    visible=is_supervisor
)
async def sv_queue(message: IncomingMessage, bot: Bot) -> None:
    """Показывает очередь NEW/OPEN и что распределено на специалистов."""
    ticket_service = get_ticket_service()
    support_service = get_support_service()

    # Очередь
    queue_tickets = await ticket_service.get_open_tickets_list()
    # В работе
    in_work_tickets = await ticket_service.get_in_work_tickets_list()

    lines: list[str] = []
    pending_tickets = await ticket_service.get_pending_tickets_list()
    # ... после вывода in_work_tickets ...
    if pending_tickets:
        lines.append("")
        lines.append("⏸ **Отложенные (PENDING):**")
        for t in pending_tickets:
            pending_time = t.pending_since.astimezone(MSK).strftime("%d.%m %H:%M") if t.pending_since else "—"
            lines.append(f"  #{t.ticket_id} | {t.user_name or '—'} | с {pending_time}")

    if queue_tickets:
        lines.append("📋 **Очередь (NEW/OPEN):**")
        for t in queue_tickets:
            created = t.created_at.astimezone(MSK).strftime("%d.%m %H:%M")
            lines.append(f"  #{t.ticket_id} | {t.user_name or '—'} | {created} | {t.status.value}")
    else:
        lines.append("📋 Очередь пуста ✅")

    lines.append("")

    if in_work_tickets:
        lines.append("🔧 **В работе (IN_WORK):**")
        for t in in_work_tickets:
            created = t.created_at.astimezone(MSK).strftime("%d.%m %H:%M")
            assigned = str(t.assigned_to)[:8] if t.assigned_to else "—"
            lines.append(f"  #{t.ticket_id} | {t.user_name or '—'} | {assigned}… | {created}")
    else:
        lines.append("🔧 В работе ничего нет")

    await bot.answer_message("\n".join(lines),wait_callback=False)


@supervisor_collector.command(
    "/sv_sla",
    description="SLA всех активных тикетов",
    visible=is_supervisor
)
async def sv_sla(message: IncomingMessage, bot: Bot) -> None:
    """Показывает SLA-статус всех активных тикетов."""
    ticket_service = get_ticket_service()

    # Собираем все активные тикеты
    open_tickets = await ticket_service.get_open_tickets_list()
    in_work_tickets = await ticket_service.get_in_work_tickets_list()
    all_tickets = list(open_tickets) + list(in_work_tickets)

    if not all_tickets:
        await bot.answer_message("Нет активных тикетов для проверки SLA.",wait_callback=False)
        return

    lines: list[str] = ["⏱ **SLA-мониторинг:**\n"]
    now = datetime.now(timezone.utc)

    for t in all_tickets:
        status_icon = {
            TicketStatus.NEW: "🆕",
            TicketStatus.OPEN: "📂",
            TicketStatus.IN_WORK: "🔧",
        }.get(t.status, "❓")

        if t.sla_deadline:
            remaining = t.sla_deadline - now
            hours_left = remaining.total_seconds() / 3600

            if t.sla_breached:
                sla_str = "🔴 BREACHED"
            elif hours_left <= 1:
                sla_str = f"🟠 {hours_left:.1f}ч"
            elif hours_left <= 4:
                sla_str = f"🟡 {hours_left:.1f}ч"
            else:
                sla_str = f"🟢 {hours_left:.1f}ч"
        else:
            sla_str = "⚪ нет SLA"

        lines.append(
            f"{status_icon} #{t.ticket_id} | {t.user_name or '—'} | "
            f"{t.status.value} | SLA: {sla_str}"
        )

    await bot.answer_message("\n".join(lines),wait_callback=False)


@supervisor_collector.command("/sv_transfer", description="Передать тикет специалисту", visible=is_supervisor)
async def sv_transfer(message: IncomingMessage, bot: Bot) -> None:
    """Показывает тикеты в работе для передачи (переиспользуем логику /transfer_to_specialist)."""
    ticket_service = get_ticket_service()
    tickets = await ticket_service.get_in_work_tickets_list()

    if not tickets:
        await bot.answer_message("Нет заявок в работе.")
        return

    bubbles = BubbleMarkup()
    for t in tickets:
        bubbles.add_button(
            command=f"/sv_do_transfer {t.ticket_id}",
            label=f"#{t.ticket_id} | {t.user_name or '—'}",
        )
    await bot.answer_message("Тикеты в работе (выберите для передачи):", bubbles=bubbles,wait_callback=False)


@supervisor_collector.command("/sv_do_transfer", description="Выбор специалиста для передачи", visible=False)
async def sv_do_transfer(message: IncomingMessage, bot: Bot) -> None:
    """Показывает онлайн-специалистов для передачи конкретного тикета."""
    args = message.body.split()
    if len(args) < 2:
        return

    try:
        ticket_id = int(args[1])
    except ValueError:
        return

    support_service = get_support_service()
    online_specialists = await support_service.get_online_specialist()

    if not online_specialists:
        await bot.answer_message("Нет специалистов в сети.")
        return

    bubbles = BubbleMarkup()
    lines = [f"Кому передать тикет #{ticket_id}?\n"]

    for spec in online_specialists:
        lines.append(f"• {spec.full_name}")
        bubbles.add_button(
            command=f"/sv_confirm_transfer {ticket_id} {spec.support_id}",
            label=f"→ {spec.full_name}",
        )

    await bot.answer_message("\n".join(lines), bubbles=bubbles, wait_callback=False)


@supervisor_collector.command("/sv_confirm_transfer", description="Подтверждение передачи", visible=False)
async def sv_confirm_transfer(message: IncomingMessage, bot: Bot) -> None:
    """Выполняет передачу тикета (переиспользуем TicketTransferService)."""
    args = message.body.split()
    if len(args) < 3:
        return

    try:
        ticket_id = int(args[1])
        to_huid = uuid.UUID(args[2])
    except (ValueError, TypeError):
        return

    ticket_service = get_ticket_service()
    support_service = get_support_service()
    transfer_service = get_transfer_service()

    ticket = await ticket_service.get_ticket_by_id(ticket_id)
    if not ticket:
        await bot.answer_message(f"Тикет #{ticket_id} не найден.",wait_callback=False)
        return

    target_spec = await support_service.get_support_by_id(to_huid)
    source_spec = (
        await support_service.get_support_by_id(ticket.assigned_to)
        if ticket.assigned_to else None
    )

    shift_service = get_shift_service()
    await shift_service.end_ticket_work(
        ticket_id=ticket_id, reason=WorkSessionEndReason.TRANSFERRED.value,
    )
    await shift_service.start_ticket_work(
        ticket_id=ticket_id, support_id=to_huid,
    )

    await bot.remove_users_from_chat(
        bot_id=uuid.UUID(BotConfig.BOT_ID),
        chat_id=ticket.support_thread_message_id,
        huids=[ticket.assigned_to, ]
    )

    # 1. Добавляем нового специалиста в чат
    await ticket_service.add_support_to_chat(bot=bot, ticket=ticket, support_id=to_huid)

    # 2. Переназначаем в БД
    await ticket_service.repo.reassign_ticket(ticket_id, to_huid)

    # 3. Аудит
    from_huid = source_spec.support_id if source_spec else message.sender.huid
    await transfer_service.set(
        ticket_id=ticket_id,
        from_huid=from_huid,
        to_huid=to_huid,
        comment=f"Перевод супервизором: {message.sender.username}",
    )

    # 4. Уведомление в тред
    if ticket.support_thread_message_id:
        await bot.send_message(
            bot_id=uuid.UUID(BotConfig.BOT_ID),
            chat_id=ticket.support_thread_message_id,
            body=(
                f"🔄 Тикет #{ticket_id} переведён супервизором.\n"
                f"{source_spec.full_name if source_spec else '—'} → "
                f"{MentionBuilder.contact(to_huid)}"
            ),
            wait_callback=False
        )

    await bot.answer_message(
        f"✅ Тикет #{ticket_id} передан → {target_spec.full_name if target_spec else to_huid}"
    )

    # [AUDIT] Перевод супервизором
    await get_audit_service().supervisor_action(
        AuditAction.TICKET_TRANSFERRED,
        ticket_id=ticket_id,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        context={
            "from_huid": str(from_huid),
            "to_huid": str(to_huid),
            "source": "supervisor",
        },
    )


@supervisor_collector.command("/sv_join", description="Подключиться к тикету (инцидент)", visible=is_supervisor)
async def sv_join(message: IncomingMessage, bot: Bot) -> None:
    """Супервизор подключается к треду тикета для разбора инцидента."""
    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /sv_join ID",wait_callback=False)
        return

    try:
        ticket_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID тикета должен быть числом.",wait_callback=False)
        return

    ticket_service = get_ticket_service()
    ticket = await ticket_service.get_ticket_by_id(ticket_id)

    if not ticket:
        await bot.answer_message(f"❌ Тикет #{ticket_id} не найден.",wait_callback=False)
        return

    # Добавляем супервизора в тред
    await bot.add_users_to_chat(
        bot_id=uuid.UUID(BotConfig.BOT_ID),
        chat_id=ticket.support_thread_message_id,
        huids=[message.sender.huid],
    )

    # [AUDIT] Подключение супервизора к треду
    await get_audit_service().supervisor_action(
        AuditAction.SUPERVISOR_JOINED_TICKET,
        ticket_id=ticket_id,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        context={"thread_id": str(ticket.support_thread_message_id)},
    )

    await bot.answer_message(f"✅ Вы подключены к чату тикета #{ticket_id}.",wait_callback=False)


@supervisor_collector.command("/sv_list", description="Все активные тикеты", visible=is_supervisor)
async def sv_list(message: IncomingMessage, bot: Bot) -> None:
    """Полный список всех активных тикетов с деталями."""
    ticket_service = get_ticket_service()

    open_tickets = await ticket_service.get_open_tickets_list()
    in_work_tickets = await ticket_service.get_in_work_tickets_list()
    all_tickets = list(open_tickets) + list(in_work_tickets)

    if not all_tickets:
        await bot.answer_message("Нет активных тикетов.",wait_callback=False)
        return

    lines: list[str] = ["📊 **Все активные тикеты:**\n"]
    pending_tickets = await ticket_service.get_pending_tickets_list()
    # ... после вывода in_work_tickets ...
    if pending_tickets:
        lines.append("")
        lines.append("⏸ **Отложенные (PENDING):**")
        for t in pending_tickets:
            pending_time = t.pending_since.astimezone(MSK).strftime("%d.%m %H:%M") if t.pending_since else "—"
            lines.append(f"  #{t.ticket_id} | {t.user_name or '—'} | с {pending_time}")

    for t in all_tickets:
        created = t.created_at.astimezone(MSK).strftime("%d.%m %H:%M")
        assigned_str = ""
        if t.assigned_to:
            support_service = get_support_service()
            spec = await support_service.get_support_by_id(t.assigned_to)
            assigned_str = f" → {spec.full_name}" if spec else f" → {str(t.assigned_to)[:8]}…"

        sla_str = ""
        if t.sla_deadline:
            sla_str = f" | SLA: {t.sla_deadline.astimezone(MSK).strftime('%H:%M')}"
            if t.sla_breached:
                sla_str += " 🔴"

        lines.append(
            f"#{t.ticket_id} | {t.user_name or '—'} | {t.status.value}{assigned_str} | "
            f"{created}{sla_str}"
        )

    await bot.answer_message("\n".join(lines),wait_callback=False)


@supervisor_collector.command("/sv_spec_list", description="Все специалисты в сети", visible=is_supervisor)
async def sv_spec_list(message: IncomingMessage, bot: Bot) -> None:
    """Выводит список всех сотрудников в сети для того чтоб поменять статус"""
    support_service = get_support_service()
    online_specialists = await support_service.get_online_specialist()

    if not online_specialists:
        await bot.answer_message("Нет специалистов в сети.",wait_callback=False)
        return

    bubbles = BubbleMarkup()
    lines = [f"Кому поменять статус ?\n"]

    for spec in online_specialists:
        lines.append(f"• {spec.full_name}")
        bubbles.add_button(
            command=f"/sv_status_change {spec.support_id}",
            label=f"→ {spec.full_name} * {spec.status.value}",
        )

    await bot.answer_message("\n".join(lines), bubbles=bubbles,wait_callback=False)


@supervisor_collector.command("/sv_status_change", description="Выбираем статус специалиста", visible=False)
async def sv_status_change(message: IncomingMessage, bot: Bot) -> None:
    """Меняем статус сотруднику"""
    args = message.body.split()

    # support_id = uuid.UUID(args[1])
    bubbles = BubbleMarkup()
    lines = "На какой статус ?"

    bubbles.add_button(
        command=f"/sv_status_set {args[1]} 1",
        label=f"→ ONLINE",
    )
    bubbles.add_button(
        command=f"/sv_status_set {args[1]} 0",
        label=f"→ OFFLINE",
    )
    bubbles.add_button(
        command=f"/sv_status_set {args[1]} 2",
        label=f"→ {SupportStatus.BREAK}",
    )
    bubbles.add_button(
        command=f"/sv_status_set {args[1]} 3",
        label=f"→ INCIDENT",
    )
    bubbles.add_button(
        command=f"/sv_status_set {args[1]} 4",
        label=f"→ TRAINING",
    )

    await bot.answer_message(body=lines, bubbles=bubbles,wait_callback=False)

@supervisor_collector.command("/sv_status_set", description="Устанавливаем статус для специалиста", visible=False)
async def sv_status_set(message: IncomingMessage, bot: Bot) -> None:
    """Устанавливаем статус специалисту"""
    args = message.body.split()

    support_id = uuid.UUID(args[1])
    status = int(args[2])
    map_status: dict = {
        0 : SupportStatus.OFFLINE,
        1 : SupportStatus.ONLINE,
        2 : SupportStatus.BREAK,
        3 : SupportStatus.INCIDENT,
        4 : SupportStatus.TRAINING
    }
    support_service = get_support_service()
    _support = await support_service.get_support_by_id(support_id=support_id)
    previous_status = _support.status.value if _support else None
    await support_service.set_status(support_id=support_id, status=map_status[status])

    # [AUDIT] Смена статуса супервизором
    await get_audit_service().log(
        action=AuditAction.SUPPORT_STATUS_CHANGED,
        actor_type="supervisor",
        entity_type="support",
        entity_id=str(support_id),
        actor_huid=message.sender.huid,
        actor_name=message.sender.username,
        old_value={"status": previous_status},
        new_value={"status": map_status[status].value},
    )


    await bot.answer_message(
        body=f"Статус {_support.full_name} изменен c {_support.status.value} на {map_status[status]}",wait_callback=False)


@supervisor_collector.command("/sv_request_ticket", description="Устанавливаем статус для специалиста", visible=False)
async def sv_request_ticket(message: IncomingMessage, bot: Bot) -> None:

    """Показывает тикеты в работе для закрытия (переиспользуем логику /transfer_to_specialist)."""
    ticket_service = get_ticket_service()
    tickets = await ticket_service.get_in_work_tickets_list()

    if not tickets:
            await bot.answer_message("Нет заявок в работе.")
            return

    bubbles = BubbleMarkup()
    for t in tickets:
            bubbles.add_button(
                command=f"/sv_close_ticket {t.ticket_id}",
                label=f"#{t.ticket_id} | {t.user_name or '—'}",
            )
    await bot.answer_message("Тикеты в работе (выберите для закрытия):", bubbles=bubbles,wait_callback=False)

@supervisor_collector.command("/sv_close_ticket", description="закрываем тикет вручную", visible=False)
async def sv_close_ticket(message: IncomingMessage, bot: Bot) -> None:

    """Закрываем тикет вручную руководителем"""
    args = message.body.split()

    ticket_id = int(args[1])
    ticket_service = get_ticket_service()
    ticket = await ticket_service.get_ticket_by_id(ticket_id=ticket_id)

    await ticket_service.repo.close_ticket(ticket_id=ticket_id)
    timer = 5

    shift_service = get_shift_service()
    await shift_service.end_ticket_work(
        ticket_id=ticket_id,
        reason=WorkSessionEndReason.SUPERVISOR_CLOSED.value,
    )

    # [AUDIT] Ручное закрытие супервизором — WARNING, т.к. это обход нормального флоу
    await get_audit_service().supervisor_action(
        AuditAction.TICKET_CLOSED_BY_SUPERVISOR,
        ticket_id=ticket_id,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        severity=AuditSeverity.WARNING,
        context={"previous_status": ticket.status.value},
    )


    # не осталяем пустой комментарий при закрытии
    await ticket_service.set_comment(ticket_id=ticket.ticket_id, comment='.')
    await asyncio.sleep(timer)

    try:
        chat_info = await bot.chat_info(bot_id=BotConfig.BOT_ID, chat_id=ticket.support_thread_message_id)
        specialists_to_remove = [
            member.huid for member in chat_info.members
            if member.kind != UserKinds.BOT
        ]

        if specialists_to_remove:
            await bot.remove_users_from_chat(
                bot_id=BotConfig.BOT_ID,
                chat_id=ticket.support_thread_message_id,
                huids=specialists_to_remove
            )
    except Exception as e:
        logger.error(f"[SERVICE] Ошибка при очистке чата #{ticket.support_thread_message_id}: {e}")

    await bot.answer_message(f"Тикет {ticket_id} закрыт вручную",wait_callback=False)


@supervisor_collector.command(
    "/sv_audit",
    description="Аудит по тикету",
    visible=is_supervisor,
)
async def sv_audit(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /sv_audit <ticket_id>",wait_callback=False)
        return

    try:
        ticket_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID тикета должен быть числом.",wait_callback=False)
        return

    records = await get_audit_service().repo.get_by_entity(
        "ticket", ticket_id, limit=200,
    )

    if not records:
        await bot.answer_message(f"📭 Аудит по тикету #{ticket_id} пуст.",wait_callback=False)
        return

    # Заголовок
    header = (
        f"📜 **Аудит тикета #{ticket_id}**\n"
        f"Всего событий: {len(records)}\n"
        f"─────────────────────────\n"
    )

    # События (идут в обратном хронологическом порядке из репо)
    lines = []
    for r in records:
        ts = r.created_at.astimezone(MSK).strftime("%d.%m %H:%M:%S")
        actor = r.actor_name or r.actor_type.value
        icon = {
            AuditSeverity.INFO: "•",
            AuditSeverity.WARNING: "⚠️",
            AuditSeverity.CRITICAL: "🔴",
        }.get(r.severity, "•")

        line = f"{icon} `{ts}` — {actor}: `{r.action}`"

        # Показываем дельту статуса, если есть
        if r.old_value and r.new_value:
            old_s = r.old_value.get("status")
            new_s = r.new_value.get("status")
            if old_s and new_s and old_s != new_s:
                line += f"\n    {old_s} → {new_s}"

        lines.append(line)

    body = header + "\n".join(lines)

    # Разбиваем на части, если длинно (лимит pybotx ~4000 символов)
    for chunk in _split_message(body, max_len=3800):
        await bot.answer_message(chunk,wait_callback=False)

    await get_audit_service().log(
        action="supervisor.viewed_audit",
        actor_type="supervisor",
        entity_type="ticket",
        entity_id=ticket_id,
        actor_huid=message.sender.huid,
        actor_name=message.sender.username,
        context={"command": "/sv_audit"},
    )


def _split_message(text: str, max_len: int = 3800) -> list[str]:
    """Разбивает длинный текст по строкам на части."""
    if len(text) <= max_len:
        return [text]
    chunks, current = [], ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > max_len:
            chunks.append(current)
            current = line
        else:
            current = (current + "\n" + line) if current else line
    if current:
        chunks.append(current)
    return chunks


@supervisor_collector.command(
    "/sv_audit_actor",
    description="Аудит по сотруднику (ФИО или HUID)",
    visible=is_supervisor,
)
async def sv_audit_actor(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()

    if len(args) < 2:
        await bot.answer_message(
            "❌ Использование: `/sv_audit_actor <ФИО или HUID> [limit]`\n"
            "Примеры:\n"
            "• `/sv_audit_actor Иванов`\n"
            "• `/sv_audit_actor Иванов Иван`\n"
            "• `/sv_audit_actor Иванов 200`\n"
            "• `/sv_audit_actor 856ac747-d98b-5cca-8619-80b9e7b6b58b`",wait_callback=False
        )
        return

    # ── Парсим limit (последний токен — если число) ────────
    body = args[1:]
    limit = AUDIT_ACTOR_DEFAULT_LIMIT
    if body[-1].isdigit():
        limit = max(1, min(int(body[-1]), AUDIT_ACTOR_MAX_LIMIT))
        body = body[:-1]

    if not body:
        await bot.answer_message("❌ Укажите ФИО или HUID сотрудника.",wait_callback=False)
        return

    query = " ".join(body).strip()
    repo = get_audit_service().repo

    # ── Определяем: HUID или ФИО ───────────────────────────
    actor_huid: uuid.UUID | None = None
    try:
        actor_huid = uuid.UUID(query)
    except (ValueError, TypeError):
        actor_huid = None

    if actor_huid is not None:
        # Точный поиск по HUID
        records = await repo.get_by_actor(actor_huid, limit=limit)
        search_label = str(actor_huid)[:8] + "…"
        matched_names = sorted({r.actor_name for r in records if r.actor_name})
    else:
        # Поиск по ФИО (подстрока, case-insensitive)
        # Сначала — проверяем, сколько уникальных сотрудников попадает
        candidates = await repo.get_actor_huids_by_name(query, limit=50)

        # Если найдено >1 сотрудника — предложим выбор кнопками
        if len(candidates) > 1:
            await _send_actor_selection(bot, query, candidates)
            return

        # 0 или 1 — тянем события
        records = await repo.get_by_actor_name(query, limit=limit)
        search_label = query
        matched_names = sorted({r.actor_name for r in records if r.actor_name})

    # ── Ничего не нашли ────────────────────────────────────
    if not records:
        await bot.answer_message(
            f"📭 По сотруднику «{search_label}» событий не найдено.",wait_callback=False
        )
        return

    # ── Формируем ответ ────────────────────────────────────
    display_name = matched_names[0] if matched_names else "—"

    lines = [
        f"📜 **Аудит сотрудника**",
        f"👤 {display_name}",
        f"Всего событий (послед. {limit}): {len(records)}",
        f"─────────────────────────",
    ]

    for r in records:
        ts = r.created_at.astimezone(MSK).strftime("%d.%m %H:%M:%S")
        icon = {
            AuditSeverity.INFO: "•",
            AuditSeverity.WARNING: "⚠️",
            AuditSeverity.CRITICAL: "🔴",
        }.get(r.severity, "•")

        entity = ""
        if r.entity_type == "ticket":
            entity = f" [#{r.entity_id}]"

        lines.append(f"{icon} `{ts}` — `{r.action}`{entity}")

    for chunk in _split_message("\n".join(lines)):
        await bot.answer_message(chunk,wait_callback=False)

    await get_audit_service().log(
        action=AuditAction.SUPERVISOR_VIEWED_AUDIT_ACTOR,
        actor_type="supervisor",
        entity_type="support",
        entity_id=str(actor_huid) if actor_huid else query,
        actor_huid=message.sender.huid,
        actor_name=message.sender.username,
        context={
            "query": query,
            "mode": "huid" if actor_huid else "name",
            "found": len(records),
        },
    )

async def _send_actor_selection(
    bot: Bot,
    query: str,
    candidates: list[tuple],
) -> None:
    """Показывает кнопки для выбора конкретного сотрудника."""
    bubbles = BubbleMarkup()
    lines = [
        f"🔍 По запросу «{query}» найдено несколько сотрудников:",
        "",
    ]

    for huid, name in candidates[:20]:
        lines.append(f"• {name}")
        bubbles.add_button(
            command=f"/sv_audit_actor {huid}",
            label=f"→ {name}",
        )

    if len(candidates) > 20:
        lines.append(f"...и ещё {len(candidates) - 20}. Уточните запрос.")

    await bot.answer_message("\n".join(lines), bubbles=bubbles,wait_callback=False)

@supervisor_collector.command(
    "/sv_audit_corr",
    description="Аудит по correlation_id",
    visible=False,  # нужна только для расследований, не светим в меню
)
async def sv_audit_corr(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /sv_audit_corr <correlation_id>",wait_callback=False)
        return

    try:
        cid = uuid.UUID(args[1])
    except (ValueError, TypeError):
        await bot.answer_message("❌ correlation_id должен быть валидным UUID.",wait_callback=False)
        return

    records = await get_audit_service().repo.get_by_correlation(cid, limit=200)

    if not records:
        await bot.answer_message(f"📭 Событий с correlation_id={cid} нет.",wait_callback=False)
        return

    lines = [
        f"🔗 **Цепочка событий** `{cid}`\n"
        f"Всего: {len(records)}\n"
        f"─────────────────────────"
    ]

    for r in records:
        ts = r.created_at.astimezone(MSK).strftime("%H:%M:%S.%f")[:-3]
        actor = r.actor_name or r.actor_type.value
        lines.append(f"`{ts}` — {actor} → `{r.action}`")

        # Показываем контекст, если есть
        if r.context:
            ctx_keys = ", ".join(f"{k}={v}" for k, v in list(r.context.items())[:3])
            lines.append(f"    контекст: {ctx_keys}")

    for chunk in _split_message("\n".join(lines)):
        await bot.answer_message(chunk,wait_callback=False)

    await get_audit_service().log(
        action="supervisor.viewed_audit",
        actor_type="supervisor",
        entity_type="support",
        entity_id=cid,
        actor_huid=message.sender.huid,
        actor_name=message.sender.username,
        context={"command": "/sv_audit"},
    )

@supervisor_collector.command(
    "/sv_audit_recent",
    description="Последние события аудита",
    visible=is_supervisor,
)
async def sv_audit_recent(message: IncomingMessage, bot: Bot) -> None:
    # ── 1. Парсинг limit ────────────────────────────────────
    args = message.body.split()
    limit = 20
    if len(args) >= 2:
        try:
            limit = max(1, min(int(args[1]), 100))
        except ValueError:
            await bot.answer_message("❌ N должно быть числом (1..100).",wait_callback=False)
            return

    # ── 2. Загрузка событий через сервис ────────────────────
    try:
        records = await get_audit_service().get_recent_events(limit=limit)
    except Exception as exc:
        logger.exception(f"[/sv_audit_recent] Ошибка загрузки: {exc}")
        await bot.answer_message(
            "❌ Не удалось загрузить события аудита. Попробуйте позже.",wait_callback=False
        )
        return

    if not records:
        await bot.answer_message("📭 Аудит пуст.",wait_callback=False)
        return

    # ── 3. Формирование ответа ──────────────────────────────
    lines = [
        f"📊 **Последние {len(records)} событий аудита**\n"
        f"─────────────────────────"
    ]

    for r in records:
        ts = r.created_at.astimezone(MSK).strftime("%d.%m %H:%M:%S")
        actor = r.actor_name or r.actor_type.value

        icon = {
            AuditSeverity.INFO: "•",
            AuditSeverity.WARNING: "⚠️",
            AuditSeverity.CRITICAL: "🔴",
        }.get(r.severity, "•")

        entity = f" [#{r.entity_id}]" if r.entity_type == "ticket" else ""
        lines.append(f"{icon} `{ts}` — {actor}: `{r.action}`{entity}")

    for chunk in _split_message("\n".join(lines)):
        await bot.answer_message(chunk,wait_callback=False)

    # ── 4. Аудит самого просмотра ───────────────────────────
    try:
        await get_audit_service().log(
            action="supervisor.viewed_audit",
            actor_type="supervisor",
            entity_type="audit",
            entity_id="recent",
            actor_huid=message.sender.huid,
            actor_name=message.sender.username,
            context={
                "command": "/sv_audit_recent",
                "limit": limit,
                "returned": len(records),
            },
        )
    except Exception as exc:
        # Просмотр удался, аудит упал — не критично
        logger.warning(f"[/sv_audit_recent] Аудит просмотра не записан: {exc}")


@supervisor_collector.command(
    "/sv_approve_reg",
    description="Одобрить регистрацию специалиста",
    visible=False,
)
async def sv_approve_reg(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /sv_approve_reg <request_id>",wait_callback=False)
        return

    try:
        request_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID заявки должен быть числом.",wait_callback=False)
        return

    support_service = get_support_service()

    result = await support_service.approve_registration(
        request_id=request_id,
        supervisor_huid=message.sender.huid,
    )

    if result is None:
        await bot.answer_message(
            f"⚠️ Заявка #{request_id} уже обработана или не существует.",wait_callback=False
        )
        return

    await invalidate_support_cache()

    specialist, request = result
    # ── Уведомляем заявителя в его личный чат ───────────────
    if request.chat_id:
        try:
            await bot.send_message(
                bot_id=uuid.UUID(BotConfig.BOT_ID),
                chat_id=request.chat_id,
                body=(
                    "✅ Ваш доступ к системе поддержки одобрен!\n"
                    "Напишите `/online`, чтобы начать смену."
                ),
                wait_callback = False
            )
        except Exception as exc:
            logger.error(
                f"[REGISTRATION] Не удалось уведомить {request.chat_id}: {exc}"
            )
    else:
        logger.warning(
            f"[REGISTRATION] Заявка #{request_id}: chat_id не сохранён"
        )

    # ── Аудит ───────────────────────────────────────────────
    await get_audit_service().supervisor_action(
        AuditAction.SUPPORT_REGISTRATION_APPROVED,
        ticket_id=0,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        context={
            "request_id": request_id,
            "support_id": str(specialist.support_id),
            "support_name": specialist.full_name,
            "chat_id": str(request.chat_id) if request.chat_id else None,
        },
    )

    await bot.answer_message(
        f"✅ Заявка #{request_id} одобрена.\n"
        f"👤 {specialist.full_name} добавлен в состав специалистов.",wait_callback=False
    )


@supervisor_collector.command(
    "/sv_reject_reg",
    description="Отклонить регистрацию специалиста",
    visible=False,
)
async def sv_reject_reg(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message(
            "❌ Использование: /sv_reject_reg <request_id> [причина]",wait_callback=False
        )
        return

    try:
        request_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID заявки должен быть числом.",wait_callback=False)
        return

    reason = " ".join(args[2:]).strip() or "Без указания причины"

    support_service = get_support_service()
    request = await support_service.reject_registration(
        request_id=request_id,
        supervisor_huid=message.sender.huid,
        reason=reason,
    )

    if request is None:
        await bot.answer_message(
            f"⚠️ Заявка #{request_id} уже обработана или не существует.",wait_callback=False
        )
        return

    # ── Уведомляем заявителя в его личный чат ───────────────
    if request.chat_id:
        try:
            await bot.send_message(
                bot_id=uuid.UUID(BotConfig.BOT_ID),
                chat_id=request.chat_id,
                body=(
                    f"❌ Ваша заявка на регистрацию отклонена.\n"
                    f"Причина: {reason}"
                ),
                wait_callback=False
            )
        except Exception as exc:
            logger.error(
                f"[REGISTRATION] Не удалось уведомить {request.chat_id}: {exc}"
            )
    else:
        logger.warning(
            f"[REGISTRATION] Заявка #{request_id}: chat_id не сохранён"
        )

    # ── Аудит ───────────────────────────────────────────────
    await get_audit_service().supervisor_action(
        AuditAction.SUPPORT_REGISTRATION_REJECTED,
        ticket_id=0,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        severity=AuditSeverity.WARNING,
        context={
            "request_id": request_id,
            "support_id": str(request.support_id),
            "chat_id": str(request.chat_id) if request.chat_id else None,
            "reason": reason,
        },
    )

    await bot.answer_message(f"✅ Заявка #{request_id} отклонена.", wait_callback=False)


@supervisor_collector.command(
    "/sv_pending_reg",
    description="Список активных заявок на регистрацию",
    visible=False,
)
async def sv_pending_reg(message: IncomingMessage, bot: Bot) -> None:
    if not await _is_registered_supervisor(message.sender.huid):
        await bot.answer_message("🚫 Только для действующих супервизоров.",wait_callback=False)
        return

    support_service = get_support_service()
    pending = await support_service.get_pending_registrations()

    if not pending:
        await bot.answer_message("📭 Активных заявок на регистрацию нет.",wait_callback=False)
        return

    await _render_pending_registrations(bot, message, pending)


async def _render_pending_registrations(
    bot: Bot,
    message: IncomingMessage,
    pending: list,
) -> None:
    """
    Рендерит список заявок с кнопками одобрения/отклонения.
    Вызывается и из /sv_pending_reg, и из /sv_start при наличии заявок.
    """
    MAX_SHOWN = 20

    lines = [f"📋 **Заявки на регистрацию ({len(pending)}):**\n"]
    bubbles = BubbleMarkup()

    for req in pending[:MAX_SHOWN]:
        ts = req.created_at.astimezone(MSK).strftime("%d.%m %H:%M")
        lines.append(f"• `#{req.id}` — {req.full_name} — {ts}")

        # Короткое имя для label (лимит ~40 символов на кнопку в eXpress)
        short_name = req.full_name[:20]

        bubbles.add_button(
            command=f"/sv_approve_reg {req.id}",
            label=f"✅ #{req.id} {short_name}",
        )
        bubbles.add_button(
            command=f"/sv_reject_reg {req.id}",
            label=f"❌ #{req.id} {short_name}",
            new_row=False,
        )

    if len(pending) > MAX_SHOWN:
        lines.append(f"\n...и ещё {len(pending) - MAX_SHOWN}")

    await bot.answer_message("\n".join(lines), bubbles=bubbles,wait_callback=False)

@supervisor_collector.command(
    "/sv_upload_specialists",
    description="Массовая загрузка специалистов из CSV",
    visible=is_supervisor,
)
async def sv_upload_specialists(message: IncomingMessage, bot: Bot) -> None:
    _awaiting_upload[message.sender.huid] = time.time()

    await bot.answer_message(
        "📎 **Загрузка списка специалистов**\n\n"
        "Отправьте CSV-файл с колонками:\n"
        "`huid;full_name`\n\n"
        "Пример:\n"
        "```\n"
        "huid;full_name\n"
        "856ac747-d98b-5cca-8619-80b9e7b6b58b;Иванов Иван Иванович\n"
        "a1b2c3d4-1111-2222-3333-444444444444;Петров Пётр\n"
        "```\n\n"
        f"• Разделитель: `;` или `,`\n"
        f"• Максимум: {RegistrationConfig.MAX_BULK_SIZE} записей\n"
        f"• У вас {RegistrationConfig.UPLOAD_WINDOW_SEC // 60} минут.",
        wait_callback=False
    )


async def try_handle_supervisor_upload(
    message: IncomingMessage, bot: Bot,
) -> bool:
    """
    Пытается обработать файл от супервизора, если он в режиме загрузки.
    Возвращает True, если файл обработан (middleware не должен пускать дальше).
    """
    if not message.file:
        return False

    uid = message.sender.huid
    if uid not in _awaiting_upload:
        return False

    # Проверяем таймаут
    if time.time() - _awaiting_upload[uid] > RegistrationConfig.UPLOAD_WINDOW_SEC:
        _awaiting_upload.pop(uid, None)
        await bot.answer_message("⌛ Время ожидания файла истекло. Запустите команду снова.",wait_callback=False)
        return True

    _awaiting_upload.pop(uid, None)

    try:
        content = await _read_attachment(message.file)
        rows = _parse_specialists_csv(content)
    except Exception as exc:
        logger.exception(f"Ошибка парсинга файла: {exc}")
        await bot.answer_message(f"❌ Не удалось разобрать файл: {exc}",wait_callback=False)
        return True

    if not rows:
        await bot.answer_message("❌ Файл пуст или не содержит валидных строк.",wait_callback=False)
        return True

    if len(rows) > RegistrationConfig.MAX_BULK_SIZE:
        await bot.answer_message(
            f"❌ Слишком много записей: {len(rows)}. "
            f"Максимум: {RegistrationConfig.MAX_BULK_SIZE}.",wait_callback=False
        )
        return True

    support_service = get_support_service()
    created, updated = 0, 0

    for support_id, full_name in rows:
        existing = await support_service.get_support_by_id(support_id)
        if existing is None:
            await support_service.get_or_create(
                support_id=support_id, full_name=full_name,
            )
            created += 1
        else:
            # Обновляем имя, если изменилось
            if existing.full_name != full_name:
                existing.full_name = full_name
                updated += 1

    await invalidate_support_cache()

    # [AUDIT]
    await get_audit_service().supervisor_action(
        AuditAction.SUPPORT_BULK_UPLOADED,
        ticket_id=0,
        supervisor_huid=message.sender.huid,
        supervisor_name=message.sender.username,
        severity=AuditSeverity.WARNING,
        context={"created": created, "updated": updated, "total": len(rows)},
    )

    await bot.answer_message(
        f"✅ Загрузка завершена.\n"
        f"📥 Создано: **{created}**\n"
        f"🔄 Обновлено: **{updated}**\n"
        f"📊 Всего обработано: {len(rows)}",wait_callback=False
    )
    return True


async def _read_attachment(attachment) -> bytes:
    """Читает содержимое IncomingAttachment."""
    content = getattr(attachment, "content", None)
    if content:
        return content
    # Fallback для API, где есть только async-буфер
    from aiofiles.tempfile import NamedTemporaryFile
    async with NamedTemporaryFile("wb+") as tmp:
        await attachment.to_async_buffer(tmp)
        await tmp.seek(0)
        return await tmp.read()


def _parse_specialists_csv(content: bytes) -> list[tuple[uuid.UUID, str]]:
    """
    Парсит CSV. Поддерживает `;` и `,`.
    Первую строку-заголовок (если есть «huid») — пропускает.
    """
    import csv
    import io

    text = content.decode("utf-8-sig")
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,")
    except csv.Error:
        dialect = csv.excel
        dialect.delimiter = ";"

    reader = csv.reader(io.StringIO(text), dialect=dialect)
    rows: list[tuple[uuid.UUID, str]] = []

    for i, row in enumerate(reader):
        if len(row) < 2:
            continue
        raw_huid, raw_name = row[0].strip(), row[1].strip()

        # Пропускаем заголовок
        if i == 0 and raw_huid.lower() in ("huid", "id", "uuid"):
            continue

        if not raw_huid or not raw_name:
            continue

        try:
            support_id = uuid.UUID(raw_huid)
        except (ValueError, TypeError):
            logger.warning(f"[BULK] Пропускаю строку {i + 1}: невалидный UUID «{raw_huid}»")
            continue

        rows.append((support_id, raw_name))

    return rows

@supervisor_collector.command(
    "/sv_labor",
    description="Трудозатраты специалиста",
    visible=is_supervisor,
)
async def sv_labor(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message(
            "❌ Использование: `/sv_labor <ФИО или HUID> [дни]`\n"
            "Пример: `/sv_labor Иванов 7` — за последние 7 дней."
        )
        return

    from services.shift_service import ShiftService
    from services.support_service import SupportService
    from datetime import datetime, timedelta, timezone

    # Парсим: ФИО + опционально дни
    days = 30
    body_parts = args[1:]
    if body_parts[-1].isdigit():
        days = int(body_parts[-1])
        body_parts = body_parts[:-1]

    query = " ".join(body_parts).strip()
    if not query:
        await bot.answer_message("❌ Укажите ФИО или HUID специалиста.")
        return

    support_svc = SupportService()
    specialist = None

    # Пробуем как UUID
    try:
        specialist = await support_svc.repo.get_specialist_by_id(uuid.UUID(query))
    except (ValueError, TypeError):
        pass

    # По ФИО — ищем в support
    if specialist is None:
        # Используем существующий метод поиска
        all_specs = await support_svc.repo.get_all()  # возвращает список huid
        # Проще — прямой запрос через repo.get_specialist_by_id
        # Нужен метод get_by_name; если нет — используем список
        await bot.answer_message(
            "ℹ️ Поиск по ФИО пока не реализован. Укажите HUID."
        )
        return

    from_dt = datetime.now(timezone.utc) - timedelta(days=days)

    shift_service = ShiftService()
    report = await shift_service.get_support_labor_report(
        support_id=specialist.support_id,
        from_dt=from_dt,
    )

    lines = [
        f"📊 **Трудозатраты: {specialist.full_name}**",
        f"Период: последние {days} дней",
        f"─────────────────────────",
        f"🔢 Смен: {report['shifts_count']}",
        f"🎫 Тикетов: {report['tickets_count']}",
        f"⏱ Всего работы: **{report['total_hours']} ч**",
        "",
    ]

    # Топ-5 самых долгих тикетов
    sessions = sorted(
        report["sessions"],
        key=lambda s: s.duration_seconds or 0,
        reverse=True,
    )[:5]

    if sessions:
        lines.append("**Топ-5 по времени:**")
        for s in sessions:
            dur_min = (s.duration_seconds or 0) // 60
            lines.append(
                f"• #{s.ticket_id} — {dur_min} мин ({s.end_reason or 'активна'})"
            )

    await bot.answer_message("\n".join(lines))