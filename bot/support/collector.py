import datetime
import logging
import time
import uuid
from zoneinfo import ZoneInfo

from pybotx import (
    Bot,
    BubbleMarkup,
    HandlerCollector,
    IncomingMessage,
    MentionBuilder,
    StatusRecipient,
    KeyboardMarkup
)

from config import BotConfig, RegistrationConfig
from db.tickets.models import SupportStatus, TicketStatus, ShiftEndReason, WorkSessionEndReason
from services.support_service import SupportService
from services.ticket_service import TicketService
from services.ticket_transfer_service import TicketTransferService
from services.supervisor_service import SupervisorService
from services.permissions_cache import get_support_huids_cached, invalidate_support_cache
from services.shift_service import ShiftService
from bot.template import TemplateMessage
from services.audit_service import get_audit_service
from db.audit.models import AuditAction

logger = logging.getLogger(__name__)

collector = HandlerCollector()
_support_chat_id = uuid.UUID(BotConfig.SUPPORT_CHAT_ID)
MSK = ZoneInfo("Europe/Moscow")

# ── Lazy Singletons для сервисов ─────────────────────────────
_ticket_service: TicketService | None = None
_support_service: SupportService | None = None
_transfer_service: TicketTransferService | None = None
_template: TemplateMessage | None = None
_supervisor_service: SupervisorService | None = None
_shift_service: ShiftService | None = None

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

def get_supervisor_service() -> SupervisorService:
    global _supervisor_service
    if _supervisor_service is None:
        _supervisor_service = SupervisorService()
    return _supervisor_service

def get_transfer_service() -> TicketTransferService:
    global _transfer_service
    if _transfer_service is None:
        _transfer_service = TicketTransferService()
    return _transfer_service

def get_template() -> TemplateMessage:
    global _template
    if _template is None:
        _template = TemplateMessage()
    return _template

def get_shift_service():
    global _shift_service
    if _shift_service is None:
        _shift_service = ShiftService()
    return _shift_service

SHIFT_DURATION_OPTIONS = {
    0: "♾ Без ограничений",
    3: "⏱ 3 часа",
    4: "⏱ 4 часа",
    12: "⏱ 12 часов",
}

# ── Кэш для проверки прав ────────────────────────────────────
# Модульный rate-limit (in-memory; для multi-process — перенесите в Redis)
_last_registration_request: dict[uuid.UUID, float] = {}

async def is_support(
    status_recipient: StatusRecipient,
    bot: Bot,
) -> bool:
    """Проверяет, является ли пользователь специалистом поддержки."""
    huids = await get_support_huids_cached()
    return str(status_recipient.huid) in huids


# ── Базовые хендлеры ─────────────────────────────────────────

@collector.default_message_handler()
async def echo_handler(message: IncomingMessage, bot: Bot):
    pass

@collector.command("/whoami", description="Показать мой HUID", visible=False)
async def whoami_handler(message: IncomingMessage, bot: Bot) -> None:
    await bot.answer_message(f"Ваш HUID: `{message.sender.huid}`",wait_callback=False)

@collector.command("/start", description="Начать общение с поддержкой")
async def start_handler(message: IncomingMessage, bot: Bot) -> None:
    short_name = message.sender.username.split()
    await bot.answer_message(
        body=f"Привет, {short_name[1]}! Чем я могу помочь?\n"
             f"Просто напиши свой вопрос в этот чат.",wait_callback=False
    )


@collector.command("/status", description="Узнать статус моего обращения", visible=False)
async def status_handler(message: IncomingMessage, bot: Bot) -> None:
    ...
    # if message.chat.id != _support_chat_id:
    #     ticket_service = get_ticket_service()
    #     ticket = await ticket_service.get_open_ticket(message.sender.huid)
    #     if ticket:
    #         await bot.answer_message(f"Статус вашего обращения: {ticket.status.value}",wait_callback=False)
    #     else:
    #         await bot.answer_message("У вас нет активных обращений.",wait_callback=False)


# ── Хендлеры специалистов ────────────────────────────────────

@collector.command("/take", description="Взять тикет в работу. Использование: /take <ID>", visible=False)
async def take_handler(message: IncomingMessage, bot: Bot) -> None:
    if message.chat.id != _support_chat_id:
        return

    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Укажите ID заявки: /take <ID>",wait_callback=False)
        return  # КРИТИЧНО: прерываем выполнение

    try:
        ticket_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID заявки должен быть числом.",wait_callback=False)
        return

    ticket_service = get_ticket_service()
    specialist_huid = message.sender.huid

    # [WORK]
    shift_service = get_shift_service()
    await shift_service.end_ticket_work(
            ticket_id=ticket_id, reason=WorkSessionEndReason.TRANSFERRED.value,
    )
    await shift_service.start_ticket_work(
            ticket_id=ticket_id, support_id=specialist_huid,
    )

    await ticket_service.take_ticket(ticket_id, specialist_huid)
    support_thread_message_id = await ticket_service.get_thread_id_for_ticket(ticket_id)

    # [AUDIT] Оператор взял тикет вручную
    await get_audit_service().support_action(
        AuditAction.TICKET_TAKEN,
        ticket_id=ticket_id,
        support_huid=specialist_huid,
        support_name=message.sender.username,
        context={"thread_id": str(support_thread_message_id) if support_thread_message_id else None},
    )

    if support_thread_message_id:
        contact = MentionBuilder.contact(specialist_huid)
        await bot.send_message(
            bot_id=message.bot.id,
            chat_id=support_thread_message_id,
            body=f"{contact} взял тикет #{ticket_id} в работу.",wait_callback=False
        )


@collector.command("/take_next", description="Взять следующий тикет из очереди", visible=is_support)
async def take_next_handler(message: IncomingMessage, bot: Bot) -> None:
    if message.chat.id == _support_chat_id:
        return

    ticket_service = get_ticket_service()
    ticket = await ticket_service.repo.get_next_from_queue()

    if not ticket:
        await bot.answer_message("🎉 Очередь пуста! Можно пить кофе ☕️",wait_callback=False)
        return

    await ticket_service.assign_ticket_to_specialist(
        ticket_id=ticket.ticket_id,
        specialist_huid=message.sender.huid,
        bot=bot
    )


@collector.command("/list", description="Список открытых заявок", visible=False)
async def list_handler(message: IncomingMessage, bot: Bot) -> None:
    if message.chat.id != _support_chat_id:
        return

    ticket_service = get_ticket_service()
    tickets = await ticket_service.get_open_tickets_list()

    if not tickets:
        await bot.answer_message("Нет открытых обращений.",wait_callback=False)
        return

    bubbles = KeyboardMarkup()
    for t in tickets:
        bubbles.add_button(
            command=f"/take {t.ticket_id}",
            label=f"{t.user_name} | {t.created_at.astimezone(MSK).strftime('%d.%m %H:%M')}"
        )
    await bot.answer_message("Все открытые тикеты:", keyboard=bubbles,wait_callback=False)


@collector.command("/transfer_to_specialist", description="Список заявок в работе для передачи", visible=False)
async def show_tickets_handler(message: IncomingMessage, bot: Bot) -> None:
    if message.chat.id != _support_chat_id:
        return

    ticket_service = get_ticket_service()
    tickets = await ticket_service.get_in_work_tickets_list()

    if not tickets:
        await bot.answer_message("Нет заявок в работе.",wait_callback=False)
        return

    bubbles = KeyboardMarkup()
    for t in tickets:
        bubbles.add_button(
            command=f"/transfer {t.ticket_id}",
            label=f"->{t.ticket_id}|{t.user_name}"
        )
    await bot.answer_message("Тикеты в работе:", keyboard=bubbles,wait_callback=False)


@collector.command("/my_tickets", description="Мои активные заявки", visible=is_support)
async def my_tickets_handler(message: IncomingMessage, bot: Bot) -> None:
    if message.chat.id == _support_chat_id:
        return

    ticket_service = get_ticket_service()
    my_tickets = await ticket_service.repo.get_tickets_by_specialist(message.sender.huid)

    if not my_tickets:
        await bot.answer_message("У вас нет активных заявок.",wait_callback=False)
        return

    text = "📋 Ваши активные заявки:\n"
    bubbles = KeyboardMarkup()
    for t in my_tickets:
        bubbles.add_button(
            # ИСПРАВЛЕНО: t.ticket_id вместо несуществующего t.support_id
            label=f"#{t.ticket_id} | {t.user_name} | {t.created_at.astimezone(MSK).strftime('%H:%M')}\n",
            link="http://localhost"
        )
    await bot.send_message(
        chat_id=message.chat.id,
        body=text,
        keyboard=bubbles,
        recipients=[message.sender.huid],
        bot_id=message.bot.id,wait_callback=False
    )


@collector.command("/online", description="Начать смену", visible=is_support)
async def support_handler_online(message: IncomingMessage, bot: Bot) -> None:
    if message.chat.id == _support_chat_id:
        return
    kb = KeyboardMarkup()
    kb.add_button(
        command=f"/offline",
        label = "🏁 Закончить смену",
    )
    support = get_support_service()
    specialist = await support.get_support_by_id(message.sender.huid)
    date_msk = datetime.datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
    # ── Специалист уже в БД → обычный флоу ────────────────
    if specialist is not None:
        await support.set_status(
            support_id=message.sender.huid, status=SupportStatus.ONLINE,
        )
        await invalidate_support_cache()

        await get_audit_service().log(
            action=AuditAction.SUPPORT_ONLINE,
            actor_type="support",
            entity_type="support",
            entity_id=str(message.sender.huid),
            actor_huid=message.sender.huid,
            actor_name=message.sender.username,
            new_value={"status": "online"},
        )

        await bot.answer_message(
            body=f"✅ Смена началась | {date_msk}\n",
            keyboard=kb,wait_callback=False
        )
        return

    # ── Специалиста нет → модерация ───────────────────────
    now_ts = time.time()
    last_ts = _last_registration_request.get(message.sender.huid, 0.0)
    if now_ts - last_ts < RegistrationConfig.REQUEST_COOLDOWN_SEC:
        remaining = int(RegistrationConfig.REQUEST_COOLDOWN_SEC - (now_ts - last_ts))
        await bot.answer_message(
            f"⏳ Заявка на регистрацию уже отправлена. "
            f"Попробуйте через {remaining} сек.",wait_callback=False
        )
        return

    request = await support.request_registration(
        support_id=message.sender.huid,
        full_name=message.sender.username,
        chat_id=message.chat.id
    )

    if request is None:
        # Уже есть pending (например, после рестарта бота)
        await bot.answer_message(
            "⏳ Ваша заявка уже на рассмотрении у супервизора. "
            "Пожалуйста, дождитесь ответа.",wait_callback=False
        )
        return

    _last_registration_request[message.sender.huid] = now_ts

    # Уведомляем супервизоров
    await _notify_supervisors_about_registration(request, bot, message)

    # [AUDIT] Заявка на регистрацию
    await get_audit_service().log(
        action=AuditAction.SUPPORT_REGISTRATION_REQUESTED,
        actor_type="support",
        entity_type="support",
        entity_id=str(message.sender.huid),
        actor_huid=message.sender.huid,
        actor_name=message.sender.username,
        new_value={"status": "pending"},
        context={"request_id": request.id},
    )

    await bot.answer_message(
        "📝 Вы не зарегистрированы как специалист поддержки.\n"
        "Заявка на доступ отправлена супервизору. Ожидайте одобрения.",wait_callback=False
    )

async def _notify_supervisors_about_registration(
    request,
    bot: Bot,
    message: IncomingMessage,
) -> None:
    """Рассылает уведомление всем активным супервизорам через worker."""
    # Ленивый импорт — избегаем циклов с worker_tasks
    from worker_tasks import escalate_to_supervisors

    bubbles = KeyboardMarkup()
    bubbles.add_button(
        command=f"/sv_approve_reg {request.id}",
        label="✅ Одобрить",
        background_color="#CDFC68",
    )
    bubbles.add_button(
        command=f"/sv_reject_reg {request.id}",
        label="❌ Отклонить",
        background_color="#9E6FC3",
        new_row=False,
    )

    body = (
        f"🔔 **Новая заявка на регистрацию**\n"
        f"👤 {request.full_name}\n"
        f"🆔 `{request.support_id}`\n"
        f"⏱ {request.created_at.astimezone(MSK).strftime('%d.%m.%Y %H:%M:%S')}"
    )

    delivered = await escalate_to_supervisors(
        body=body,
        bubbles=bubbles,
    )

    if delivered == 0:
        logger.warning(
            f"[REGISTRATION] Заявка #{request.id}: "
            f"уведомление не доставлено ни одному супервизору "
            f"(нет активных или нет chat_id)"
        )


@collector.command(
    "/online_do",
    description="Начать смену с указанной длительностью",
    visible=False,
)
async def online_do_handler(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 2:
        return

    try:
        hours = int(args[1])
    except ValueError:
        return

    if hours not in SHIFT_DURATION_OPTIONS:
        await bot.answer_message("❌ Неверная длительность смены.")
        return

    support = get_support_service()
    specialist = await support.repo.get_specialist_by_id(message.sender.huid)
    if specialist is None:
        await bot.answer_message("❌ Вы не зарегистрированы как специалист.")
        return

    if specialist.status == SupportStatus.ONLINE:
        await bot.answer_message("⚠️ Вы уже в смене.")
        return

    shift_service = get_shift_service()
    shift = await shift_service.start_shift(
        support_id=message.sender.huid,
        planned_duration_hours=hours if hours > 0 else None,
    )

    if shift is None:
        await bot.answer_message("❌ Не удалось начать смену.")
        return

    date_msk = datetime.datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S")

    if hours > 0:
        ends_at = (datetime.datetime.now(MSK) + datetime.timedelta(hours=hours))
        ends_str = ends_at.strftime("%d.%m.%Y %H:%M")
        body = (
            f"✅ Смена началась | {date_msk}\n"
            f"⏱ Автозавершение: {ends_str}"
        )
    else:
        body = f"✅ Смена началась | {date_msk}\n⏱ Без ограничений"

    # [AUDIT]
    await get_audit_service().log(
        action="support.shift_started",
        actor_type="support",
        entity_type="support",
        entity_id=str(message.sender.huid),
        actor_huid=message.sender.huid,
        actor_name=message.sender.username,
        new={"status": "online", "duration_hours": hours or None},
    )

    await bot.answer_message(body,wait_callback=False)

@collector.command("/offline", description="Закончить смену", visible=is_support)
async def support_handler_offline(message: IncomingMessage, bot: Bot) -> None:
    if message.chat.id == _support_chat_id:
        return

    shift_service = get_shift_service()
    closed = await shift_service.end_shift(
        support_id=message.sender.huid,
        reason=ShiftEndReason.MANUAL.value,
    )

    # [AUDIT]
    await get_audit_service().log(
        action="support.shift_ended",
        actor_type="support",
        entity_type="support",
        entity_id=str(message.sender.huid),
        actor_huid=message.sender.huid,
        actor_name=message.sender.username,
        new={"status": "offline", "closed_sessions": closed},
    )

    date_msk = datetime.datetime.now(MSK).strftime("%d.%m.%Y %H:%M:%S")
    await bot.answer_message(
        f"🏁 Смена закончилась | {date_msk}\n"
        f"Закрыто активных тикетов: {closed}\n"
        f"Ты молодец!",
        wait_callback=False
    )

# ── Хендлеры переводов ───────────────────────────────────────

@collector.command("/receive_ticket", description="Перевести тикет", visible=False)
async def receive_ticket_handler(message: IncomingMessage, bot: Bot) -> None:
    if str(message.chat.id) != BotConfig.SUPPORT_CHAT_ID:
        return

    args = message.body.split()
    # if len(args) < 3:
    #     await bot.answer_message("❌ Использование: /receive_ticket <id> <huid> [комментарий]")
    #     return



    ticket_id = int(args[1])
    to_huid = uuid.UUID(args[2])

    # Склеиваем остаток аргументов в комментарий (защита от обрезания на пробелах)
    comment = " ".join(args[3:]) if len(args) > 3 else None

    ticket_service = get_ticket_service()
    support_service = get_support_service()
    transfer_service = get_transfer_service()

    target_spec = await support_service.get_support_by_id(support_id=to_huid)
    ticket = await ticket_service.get_ticket_by_id(ticket_id)
    source_spec = await support_service.get_support_by_id(support_id=ticket.assigned_to)

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

    # 3. Аудит перевода
    await transfer_service.set(
        ticket_id=ticket_id,
        from_huid=source_spec.support_id,
        to_huid=to_huid,
        comment=comment
    )

    await bot.send_message(
        bot_id=uuid.UUID(BotConfig.BOT_ID),
        chat_id=ticket.support_thread_message_id,
        body=(
            f"🔄 Тикет #{ticket_id} переведён.\n"
            f"{source_spec.full_name} → {MentionBuilder.contact(to_huid)}\n"
            + (f"💬 Комментарий: {comment}" if comment else "")
        ),wait_callback=False
    )

    # [AUDIT] Оператор перевёл тикет коллеге
    await get_audit_service().support_action(
        AuditAction.TICKET_TRANSFERRED,
        ticket_id=ticket_id,
        support_huid=message.sender.huid,
        support_name=message.sender.username,
        old={"assigned_to": str(source_spec.support_id) if source_spec else None},
        new={"assigned_to": str(to_huid)},
        context={"source": "operator", "comment": comment},
    )

    await bot.answer_message(f"✅ Тикет #{ticket_id} переведён на {target_spec.full_name}.",wait_callback=False)


@collector.command("/transfer", description="Перевести тикет", visible=False)
async def transfer_handler(message: IncomingMessage, bot: Bot) -> None:
    if str(message.chat.id) != BotConfig.SUPPORT_CHAT_ID:
        return

    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /transfer <id>",wait_callback=False)
        return

    try:
        ticket_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID тикета должен быть числом.",wait_callback=False)
        return

    ticket_service = get_ticket_service()
    support_service = get_support_service()

    ticket = await ticket_service.get_ticket_by_id(ticket_id)
    if not ticket:
        await bot.answer_message(f"❌ Тикет #{ticket_id} не найден.",wait_callback=False)
        return

    online_specialists = await support_service.get_online_specialist()
    # available_specs = [s for s in online_specialists if s.support_id != message.sender.huid]
    available_specs = [s for s in online_specialists]

    if not available_specs:
        await bot.answer_message("😔 Сейчас нет других специалистов в сети для перевода.",wait_callback=False)
        return

    bubbles = KeyboardMarkup()
    lines = ["👥 Специалисты в сети:\n"]

    for spec in available_specs:
        lines.append(f"• {spec.full_name}")
        bubbles.add_button(
            command=f"/receive_ticket {ticket_id} {spec.support_id}",
            label=f"-→ {spec.full_name}",
        )

    await bot.answer_message("\n".join(lines), keyboard=bubbles,wait_callback=False)


@collector.command("/history", description="История переводов", visible=False)
async def history_handler(message: IncomingMessage, bot: Bot) -> None:
    if str(message.chat.id) != BotConfig.SUPPORT_CHAT_ID:
        return

    args = message.body.split()
    if len(args) < 2:
        await bot.answer_message("❌ Использование: /history <ticket_id>",wait_callback=False)
        return

    try:
        ticket_id = int(args[1])
    except ValueError:
        await bot.answer_message("❌ ID тикета должен быть числом.",wait_callback=False)
        return

    ticket_service = get_ticket_service()
    support_service = get_support_service()
    transfer_service = get_transfer_service()

    ticket = await ticket_service.get_ticket_by_id(ticket_id)
    if not ticket:
        await bot.answer_message(f"❌ Тикет #{ticket_id} не найден.",wait_callback=False)
        return

    transfers = await transfer_service.get_history(ticket_id)
    if not transfers:
        await bot.answer_message(f"📜 Тикет #{ticket_id}: переводов не было.",wait_callback=False)
        return

    lines = [f"📜 История переводов тикета #{ticket_id}:\n"]

    for transfer in transfers:
        from_spec = await support_service.get_support_by_id(transfer.from_huid)
        to_spec = await support_service.get_support_by_id(transfer.to_huid)

        from_name = from_spec.full_name if from_spec else str(transfer.from_huid)
        to_name = to_spec.full_name if to_spec else str(transfer.to_huid)

        time_str = transfer.transferred_at.astimezone(MSK).strftime('%d.%m.%Y %H:%M')

        lines.append(f"• {time_str}")
        lines.append(f"  {from_name} → {to_name}")
        if transfer.comment:
            lines.append(f"  💬 {transfer.comment}")
        lines.append("")

    await bot.answer_message("\n".join(lines),wait_callback=False)


# ── Callback-хендлеры (Кнопки от бота) ───────────────────────

@collector.command("/additional", description="Callback: доп вопросы", visible=False)
async def additional_handler(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 3:
        return

    try:
        decision = int(args[1])
        ticket_id = int(args[2])
    except ValueError:
        return

    ticket_service = get_ticket_service()

    # ЗАЩИТА: Проверяем, не была ли кнопка уже нажата ранее
    if await ticket_service.is_additional_answered(ticket_id):
        # Опционально: можно отправить уведомление "Вы уже ответили",
        # но лучше просто молча игнорировать, чтобы не спамить пользователю.
        logger.info(f"[COLLECTOR] Повторное нажатие кнопки /additional для тикета #{ticket_id}")
        return

    #  Помечаем, что вопрос обработан (сразу, до выполнения логики)
    await ticket_service.mark_additional_answered(ticket_id)

    if decision == 0:
    #     # остались доп. вопросы — инициируем новый тикет
    #     await bot.send_message(
    #         bot_id=message.bot.id,
    #         chat_id=message.chat.id,
    #         body="Ответным сообщением напишите свой дополнительный вопрос"
    #     )
    # else:
        # Нет доп. вопросов — прощаемся
        short_name = message.sender.username.split()
        name = short_name[1]

        await bot.send_message(
            bot_id=message.bot.id,
            chat_id=message.chat.id,
            body=f"{name}, до свидания! 👋"
        )


@collector.command("/solved", description="Callback: проблема решена", visible=False)
async def problem_handler(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 3:
        return

    try:
        decision = int(args[1])
        ticket_id = int(args[2])
    except ValueError:
        return

    ticket_service = get_ticket_service()
    ticket = await ticket_service.repo.get_ticket_by_id(ticket_id)

    if not ticket:
        await bot.answer_message("Тикет не найден.",wait_callback=False)
        return

    # ── Обработка PENDING статуса ──
    if ticket.status == TicketStatus.PENDING:
        if decision:  # ДА - проблема решена
            # НЕ реактивируем, просто переводим в SOLVED
            await ticket_service.resolve_pending_ticket(ticket_id)
            # Продолжаем стандартный флоу сбора обратной связи
            await ticket_service.send_rating_request(message, ticket_id, bot)
            return
        else:  # НЕТ - проблема не решена
            # Реактивируем и возвращаем в работу
            await ticket_service.reactivate_pending_ticket(ticket_id, bot)
            # Обновляем объект ticket, так как его статус изменился
            ticket = await ticket_service.repo.get_ticket_by_id(ticket_id)

    # ── Стандартная обработка для IN_WORK ──
    if ticket.status in (TicketStatus.SOLVED, TicketStatus.CLOSED) or ticket.solution_time is not None:
        await bot.answer_message("Это Мы уже обработали. Спасибо за обратную связь!",wait_callback=False)
        return

    if decision:
        await ticket_service.handle_problem_solved(message=message, ticket_id=ticket_id, bot=bot)
        await ticket_service.send_rating_request(message, ticket_id, bot)
    else:
        await ticket_service.handle_problem_not_solved(message=message, ticket_id=ticket_id, bot=bot)


@collector.command("/rating", description="Callback: оценка", visible=False)
async def rating_handler(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 3: return

    try:
        rating = int(args[1])
        ticket_id = int(args[2])
    except ValueError:
        return

    ticket_service = get_ticket_service()

    ticket = await ticket_service.get_ticket_by_id(ticket_id=ticket_id)

    # ── Если пользователь "проснулся" и нажал кнопку, будим тикет ──
    if ticket.status == TicketStatus.PENDING:
        await ticket_service.reactivate_pending_ticket(ticket_id, bot)

    if await ticket_service.is_feedback_completed(ticket_id):
        await bot.answer_message(f"Вы уже оставили обратную связь Спасибо!",wait_callback=False)
        return

    if not await ticket_service.can_submit_rating(ticket_id):
        await bot.answer_message(f"Вы уже оценили",wait_callback=False)
        return

    await ticket_service.handle_rating(message=message, ticket_id=ticket_id, rating=rating, bot=bot)

    # [AUDIT] Оценка пользователя
    await get_audit_service().user_action(
        AuditAction.RATING_RECEIVED,
        ticket_id=ticket_id,
        user_huid=message.sender.huid,
        user_name=message.sender.username,
        new={"rating": rating},
    )


@collector.command("/comment", description="Callback: комментарий", visible=False)
async def comment_handler(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()
    if len(args) < 3: return

    try:
        decision = int(args[1])
        ticket_id = int(args[2])
    except ValueError:
        return

    ticket_service = get_ticket_service()
    ticket = await ticket_service.repo.get_ticket_by_id(ticket_id)

    # ── НОВОЕ: Если пользователь "проснулся" и нажал кнопку, будим тикет ──
    if ticket.status == TicketStatus.PENDING:
        await ticket_service.reactivate_pending_ticket(ticket_id, bot)
        
    if not await ticket_service.can_submit_comment(ticket_id):
        await bot.answer_message(f"Это Мы уже обработали. Спасибо за обратную связь!",wait_callback=False)
        return

    await ticket_service.handle_comment_request(message=message, ticket_id=ticket_id, want_comment=decision, bot=bot)
    # [AUDIT] Решение пользователя о комментарии
    await get_audit_service().user_action(
        AuditAction.COMMENT_RECEIVED,
        ticket_id=ticket_id,
        user_huid=message.sender.huid,
        user_name=message.sender.username,
        new={"want_comment": bool(decision)},
        context={"stage": "requested"},
    )


@collector.command("/request", description="Callback: запрос от спеца", visible=False)
async def support_handler(message: IncomingMessage, bot: Bot) -> None:
    args = message.body.split()

    data = args[1]

    ticket_id = int(args[2])
    ticket_service = get_ticket_service()
    template = get_template()

    bubbles = BubbleMarkup()
    bubbles.add_button(command=f"/request solved {ticket_id}", label="Запросить закрытие тикета")
    bubbles.add_button(command=f"/request check_info {ticket_id}", label="Поиск информации")
    bubbles.add_button(command=f"/request reset_password {ticket_id}", label="Сброс пароля")
    bubbles.add_button(command=f"/request template_user {ticket_id}", label="Массовый сбой")

    if data == "solved":
        date_msk = datetime.datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        await bot.answer_message(
            body=f'Отправлен запрос на закрытие тикета {date_msk}',wait_callback=False
            # bubbles=bubbles
        )
        await ticket_service.request_close_ticket(message, ticket_id, bot)

    _ticket = await ticket_service.get_ticket_by_id(ticket_id=ticket_id)

    # [AUDIT] Использование шаблонов оператором (кроме solved — там логируется внутри сервиса)
    if data in ("check_info", "reset_password", "template_user"):
        await get_audit_service().support_action(
            AuditAction.TEMPLATE_SENT,
            ticket_id=ticket_id,
            support_huid=message.sender.huid,
            support_name=message.sender.username,
            context={"template": data},
        )

    if data == 'check_info':
        date_msk = datetime.datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        await bot.answer_message(
            body=f'Отправлено уведомление на уточнение информации {date_msk}',wait_callback=False
            # bubbles=bubbles
        )
        await bot.send_message(
            bot_id=message.bot.id,
            chat_id=_ticket.chat_id,
            body=template.check_info
        )

    if data == 'reset_password':
        date_msk = datetime.datetime.now(MSK).strftime('%d.%m.%Y %H:%M:%S')
        await bot.answer_message(
            body=f'Отправлена информация о способах сброса пароля {date_msk}',wait_callback=False
            # bubbles=bubbles
        )
        await bot.send_message(
            bot_id=message.bot.id,
            chat_id=_ticket.chat_id,
            body=template.reset_password,wait_callback=False
        )

    if data == 'template_user':
        await bot.answer_message(
            body=template.template_user,wait_callback=False
            # bubbles = bubbles
        )