import asyncio
import base64
import io
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Optional, Any, Coroutine, Sequence
from uuid import UUID

from PIL import Image, ImageDraw
from pybotx import (
    Bot,
    ChatTypes, 
    IncomingMessage, 
    MentionBuilder, 
    UserKinds,
    KeyboardMarkup,
    BubbleMarkup
)

from config import BotConfig, UserResponseConfig
from db.tickets.models import Ticket, TicketStatus, SupportStatus, WorkSessionEndReason
from db.tickets.repo import TicketRepo
from services.shift_service import ShiftService
from services.sla_service import SLAService
from services.support_service import SupportService
from services.audit_service import get_audit_service
from db.audit.models import AuditAction, AuditSeverity

logger = logging.getLogger(__name__)

# --- Dependency Injection / Lazy Singletons ---
# Предотвращает спам инстанцированиями и подключениями к БД на каждое сообщение
_support_service: Optional[SupportService] = None
_sla_service: Optional[SLAService] = None
_shift_service: Optional[ShiftService] = None

def get_support_service() -> SupportService:
    global _support_service
    if _support_service is None:
        _support_service = SupportService()
    return _support_service


def get_sla_service() -> SLAService:
    global _sla_service
    if _sla_service is None:
        _sla_service = SLAService()
    return _sla_service

def get_shift_service():
    global _shift_service
    if _shift_service is None:
        _shift_service = ShiftService()
    return _shift_service

async def create_circle_avatar(status: TicketStatus, size: int = 100) -> str:
    """Создает PNG с кругом заданного цвета для аватара чата поддержки."""
    colors = {
        TicketStatus.NEW: "grey",
        TicketStatus.OPEN: "orange",
        TicketStatus.IN_WORK: "blue",
        TicketStatus.SOLVED: "green",
        TicketStatus.CLOSED: "red",
    }
    color = colors.get(status, "white")

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([0, 0, size - 1, size - 1], fill=color)

    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    img_base64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{img_base64}"


def _get_display_name(username: Optional[str]) -> str:
    """
    Безопасно извлекает имя (или фамилию) для отображения.
    Защита от IndexError, если у пользователя нет фамилии или username равен None.
    """
    if not username:
        return "Коллега"
    parts = username.split()
    # Берем фамилию, если есть, иначе имя, иначе заглушку
    if len(parts) > 1:
        return parts[1]
    return parts[0] if parts else "Коллега"


class TicketService:
    def __init__(self):
        self.repo = TicketRepo()
        # self.repo_specialist = SupportRepo()
        self._bot_id = uuid.UUID(BotConfig.BOT_ID)

    # ── Управление тикетами ───────────────────────────────────

    async def create_or_update_ticket(
            self,
            message: IncomingMessage,
            bot: Bot,
            role: str,
            user_huid: uuid.UUID,
            message_body: str,
            user_name: str,
            chat_id: uuid.UUID,
    ) -> Ticket:
        """Создает новый тикет или обновляет существующий. Устанавливает SLA для новых."""
        message_body = message.body or "[Медиа-вложение]"

        # Проверяем, есть ли уже открытый тикет
        existing_ticket = await self.repo.get_open_ticket_by_user(message.sender.huid)

        # Передаем в репозиторий только тело сообщения.
        # Репозиторий сам безопасно сделает конкатенацию с историей.
        ticket = await self.repo.create_or_update(
            user_huid=user_huid,
            message_body=message_body,
            user_name=user_name,
            chat_id=chat_id,
            role=role
        )
        # [AUDIT] Логируем только реальное создание
        if not existing_ticket:
            await get_audit_service().user_action(
                AuditAction.TICKET_CREATED,
                ticket_id=ticket.ticket_id,
                user_huid=message.sender.huid,
                user_name=message.sender.username,
                new={"status": ticket.status.value},
                context={
                    "chat_id": str(message.chat.id),
                    "has_file": bool(getattr(message, "file", None)),
                    "body_len": len(message_body),
                },
            )

        # Устанавливаем SLA дедлайн ТОЛЬКО для новых тикетов
        if not existing_ticket and ticket and not ticket.sla_deadline:
            sla_service = get_sla_service()
            deadline = sla_service.calculate_deadline(ticket.created_at or datetime.now(timezone.utc))
            await self.repo.set_sla_deadline(
                ticket_id=ticket.ticket_id,
                deadline=deadline,
            )
            logger.info(f"[SLA] Тикет #{ticket.ticket_id}: дедлайн установлен на {deadline}")

        return ticket

    async def get_open_ticket(self, user_huid: uuid.UUID) -> Optional[Ticket]:
        return await self.repo.get_open_ticket_by_user(user_huid)

    async def get_ticket_by_thread_id(self, support_thread_message_id: uuid.UUID) -> Optional[Ticket]:
        return await self.repo.get_ticket_by_thread_id(message_id=support_thread_message_id)

    async def get_ticket_by_chat_id(self, chat_id: uuid.UUID) -> Optional[Ticket]:
        return await self.repo.get_active_ticket_by_chat_id(chat_id=chat_id)

    async def get_thread_id_for_ticket(self, ticket_id: int) -> Optional[uuid.UUID]:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        return ticket.support_thread_message_id if ticket else None

    async def get_ticket_by_id(self, ticket_id: int) -> Optional[Ticket]:
        return await self.repo.get_ticket_by_id(ticket_id)

    # ── Управление чатами поддержки ───────────────────────────

    async def create_chat_for_support(self, bot: Bot, ticket: Ticket) -> uuid.UUID:
        """Создает групповой чат для специалиста и пользователя"""
        _avatar = await create_circle_avatar(status=TicketStatus.IN_WORK)

        # Используем текущее время, чтобы избежать AttributeError на ticket.created_at
        now_str = datetime.now(timezone.utc).strftime('%d.%m.%Y %H:%M:%S')

        chat_id = await bot.create_chat(
            bot_id=self._bot_id,
            name=f"{ticket.user_name or 'Пользователь'}",
            description=f"Тикет #{ticket.ticket_id}\nСоздано: {now_str}",
            chat_type=ChatTypes.GROUP_CHAT,
            huids=[self._bot_id, ticket.assigned_to] if ticket.assigned_to else [self._bot_id],
            avatar=_avatar,
            shared_history=True,

        )
        return chat_id

    async def add_support_to_chat(self, bot: Bot, ticket: Ticket, support_id: uuid.UUID) -> None:
        """Добавляет специалиста в существующий чат (тред)."""

        await bot.add_users_to_chat(
            bot_id=self._bot_id,
            chat_id=ticket.support_thread_message_id,
            huids=[support_id, ]
        )

    # ── Назначение и очередь ──────────────────────────────────

    async def get_support_chat_by_user(self, user_huid: uuid.UUID) -> UUID | None:
        return await self.repo.get_thread_by_user(user_huid=user_huid)

    async def set_support_chat_id(self, ticket_id: int, message_id: uuid.UUID):
        return await self.repo.set_support_chat_id(ticket_id=ticket_id, message_id=message_id)

    async def assign_ticket_to_specialist(
            self,
            ticket_id: int,
            specialist_huid: uuid.UUID,
            bot: Bot,
    ) -> None:
        """
        Назначает тикет на специалиста.
        - Если чат уже существует — добавляет специалиста в него.
        - Если чата нет — создаёт его, сохраняет ID в БД и добавляет специалиста.
        """
        # Назначаем тикет в БД (статус IN_WORK, assigned_to, taken_at)
        ticket = await self.repo.reassign_ticket(ticket_id, specialist_huid)

        # [AUDIT] Фиксируем факт назначения (актор — SYSTEM, т.к. это автомеханизм)
        await get_audit_service().system_action(
            AuditAction.TICKET_ASSIGNED,
            ticket_id=ticket_id,
            new={
                "status": ticket.status.value,
                "assigned_to": str(specialist_huid),
            },
            context={"source": "auto_assign" if ticket.is_priority is False else "reassign"},
        )

        # [WORK] Открываем сессию трудозатрат
        shift_service = get_shift_service()
        await shift_service.end_ticket_work(
            ticket_id=ticket_id,
            reason=WorkSessionEndReason.TRANSFERRED.value,
        )
        await shift_service.start_ticket_work(
            ticket_id=ticket_id,
            support_id=specialist_huid,
        )

        # Ищем существующий чат для этого пользователя
        support_chat_id = await self.get_support_chat_by_user(ticket.user_huid)
        logger.info(f'Получили chat_id: {support_chat_id}')  # 💡 См. совет ниже

        mention = MentionBuilder.contact(specialist_huid)
        history_text = await self._format_ticket_history(ticket)

        if support_chat_id is None:
            support_chat_id = await self.create_chat_for_support(bot, ticket)
            logger.info(f'Чат создали, так как его не было: {support_chat_id}')

        # Сохраняем ID чата в БД
        await self.set_support_chat_id(ticket_id, support_chat_id)

        # ИСПРАВЛЕНО: передаем support_chat_id, а не устаревший ticket.support_thread_message_id
        await bot.add_users_to_chat(
            bot_id=self._bot_id,
            chat_id=support_chat_id,
            huids=[specialist_huid]
        )

        bubbles = BubbleMarkup()
        bubbles.add_button(command=f"/request solved {ticket.ticket_id}", label="Запросить закрытие тикета")
        bubbles.add_button(command=f"/request check_info {ticket.ticket_id}", label="Поиск информации")
        bubbles.add_button(command=f"/request reset_password {ticket.ticket_id}", label="Сброс пароля")
        bubbles.add_button(command=f"/request template_user {ticket.ticket_id}", label="Массовый сбой")

        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=support_chat_id,
            body=(
                f"🎩 {mention} взял заявку #{ticket.ticket_id}\n"
                f"👤 Пользователь: {ticket.user_name}\n"
                f"📝 История:\n{history_text}"
            ),
            bubbles=bubbles,
            wait_callback=False
        )
        # Уведомляем пользователя в его личном чате
        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=ticket.chat_id,
            recipients=[ticket.user_huid],
            body="К вашей заявке подключился специалист. Ожидайте ответа в этом чате.",
            wait_callback=False
        )

    async def auto_assign_ticket(self, ticket_id: int, bot: Bot) -> bool | None:
        """Автоматически назначает тикет на самого свободного специалиста (лимит 2 тикета)."""
        support_service = get_support_service()
        specialists = await support_service.get_online_specialist()
        if not specialists:
            return False

        loads = []
        for spec in specialists:
            load = await self.repo.get_specialist_load(spec.support_id)
            loads.append((load, spec.support_id))

        loads.sort(key=lambda x: x[0])
        min_load, best_specialist = loads[0]

        if min_load >= 2:
            logger.info(
                f"[AUTO-ASSIGN] Все специалисты загружены (мин. нагрузка {min_load}). Тикет #{ticket_id} в очереди.")
            return False

        await self.assign_ticket_to_specialist(ticket_id, best_specialist, bot)
        return True

    async def resolve_pending_ticket(self, ticket_id: int) -> bool:
        """
        Переводит PENDING тикет в SOLVED без реактивации.
        Используется, когда пользователь подтвердил решение проблемы (нажал ДА).
        """
        resolved = await self.repo.resolve_pending_ticket(ticket_id)
        if resolved:
            logger.info(
                f"Тикет #{ticket_id} переведен из PENDING в SOLVED "
                "(пользователь подтвердил решение)"
            )
            # [AUDIT] Подтверждение решения из PENDING
            await get_audit_service().system_action(
                AuditAction.TICKET_RESOLVED_FROM_PENDING,
                ticket_id=ticket_id,
                old={"status": "pending"},
                new={"status": "solved"},
            )
        return resolved

    async def reactivate_pending_and_assign(
        self,
        ticket_id: int,
        specialist_huid: uuid.UUID,
        bot: Bot,
        supervisor_huid: uuid.UUID | None = None,
        supervisor_name: str | None = None,
    ) -> bool:
        """
        Возвращает PENDING-тикет в работу и назначает на специалиста.

        Делает всё атомарно-последовательно:
          1. Меняет статус в БД (PENDING → IN_WORK)
          2. Добавляет специалиста в тред поддержки (создаёт при отсутствии)
          3. Уведомляет специалиста в треде и пользователя в личке
          4. Пишет событие в аудит

        Возвращает True при успехе, False если тикет уже не в PENDING.
        """
        # ── 1. БД ────────────────────────────────────────────
        ticket = await self.repo.reactivate_pending_to_work(
            ticket_id, specialist_huid,
        )
        if ticket is None:
            logger.info(
                f"[REACTIVATE] Тикет #{ticket_id} не был реактивирован — "
                f"уже не в PENDING"
            )
            return False

        # ── 2. Тред поддержки ────────────────────────────────
        support_chat_id = await self.repo.get_thread_id(ticket_id)

        # Добавляем специалиста в чат
        try:
            await bot.add_users_to_chat(
                bot_id=self._bot_id,
                chat_id=support_chat_id,
                huids=[specialist_huid],
            )
        except Exception as exc:
            logger.error(
                f"[REACTIVATE] Не удалось добавить специалиста "
                f"{specialist_huid} в тред #{ticket_id}: {exc}"
            )

        # ── 3. Уведомления ───────────────────────────────────
        mention = MentionBuilder.contact(specialist_huid)
        history_text = await self._format_ticket_history(ticket)

        try:
            await bot.send_message(
                bot_id=self._bot_id,
                chat_id=support_chat_id,
                body=(
                    f"🔥 Тикет #{ticket.ticket_id} возвращён из PENDING в работу\n"
                    f"🎩 Назначен: {mention}\n"
                    f"👤 Пользователь: {ticket.user_name}\n"
                    f"📝 История:\n{history_text}"
                ),
                wait_callback=False

            )
        except Exception as exc:
            logger.exception(
                f"[REACTIVATE] Ошибка уведомления в тред #{ticket_id}: {exc}"
            )

    # ── 4. Аудит ─────────────────────────────────────────
        await get_audit_service().log(
            action=AuditAction.TICKET_REACTIVATED,
            actor_type="supervisor" if supervisor_huid else "system",
            actor_huid=supervisor_huid,
            actor_name=supervisor_name,
            entity_type="ticket",
            entity_id=ticket_id,
            old_value={"status": "pending"},
            new_value={
                "status": "in_work",
                "assigned_to": str(specialist_huid),
            },
            context={
                "source": "supervisor_manual" if supervisor_huid else "manual_assign",
            },
        )

        logger.info(
            f"[REACTIVATE] Тикет #{ticket_id} возвращён из PENDING "
            f"и назначен на {specialist_huid}"
        )
        return True

    async def process_queue(self, bot: Bot) -> int:
        """Обрабатывает очередь тикетов в статусе NEW."""
        new_tickets = await self.repo.get_all_new_tickets()
        assigned_count = 0
        support_service = get_support_service()

        for ticket in new_tickets:
            try:
                # Проверяем, есть ли свободные слоты
                specialists = await support_service.get_online_specialist()
                if not specialists:
                    break

                loads = [(await self.repo.get_specialist_load(s.support_id), s.support_id) for s in specialists]
                loads.sort(key=lambda x: x[0])

                if loads[0][0] >= 2:
                    break  # Все загружены, прерываем цикл

                await self.assign_ticket_to_specialist(ticket.ticket_id, loads[0][1], bot)
                assigned_count += 1
            except Exception as e:
                logger.exception(f"Ошибка при назначении тикета #{ticket.ticket_id}: {e}")

        return assigned_count

    async def take_ticket(self, ticket_id: int, specialist_huid: uuid.UUID) -> Optional[Ticket]:
        return await self.repo.assign_ticket(ticket_id, specialist_huid)

    # ── Маршрутизация сообщений ───────────────────────────────

    async def forward_message_to_support(
            self,
            message: IncomingMessage,
            bot: Bot,
            ticket_id: int,
            support_chat_id: uuid.UUID,
            show_buttons:bool=True
    ) -> None:
        """Пересылает сообщение пользователя из лички в тред поддержки."""
        # if not ticket.support_thread_message_id:
        #     logger.error(f"[SERVICE] У тикета #{ticket.ticket_id} нет thread_id для пересылки!")
        #     return

        bubbles = BubbleMarkup()
        bubbles.add_button(command=f"/request solved {ticket_id}", label="Запросить закрытие тикета")
        bubbles.add_button(command=f"/request check_info {ticket_id}", label="Поиск информации")
        bubbles.add_button(command=f"/request reset_password {ticket_id}", label="Сброс пароля")
        bubbles.add_button(command=f"/request template_user {ticket_id}", label="Массовый сбой")

        # Защита от None и очистка от системных тегов файлов
        body_text = message.body or ""
        clean_body = re.sub(r"\{\{file:[^}]+\}\}", "", body_text).strip()
        sender_name = message.sender.username or "Пользователь"
        if show_buttons:
            if hasattr(message, "file") and message.file:
                await bot.send_message(
                    bot_id=self._bot_id,
                    chat_id=support_chat_id,
                    body=f"{sender_name}:\n{clean_body}",
                    file=message.file,
                    bubbles=bubbles,
                    wait_callback=False
                )
            else:
                await bot.send_message(
                    bot_id=self._bot_id,
                    chat_id=support_chat_id,
                    body=f"{sender_name}:\n{clean_body or '[Медиа-вложение]'}",
                    bubbles=bubbles,
                    wait_callback=False
                )
        else:
            if hasattr(message, "file") and message.file:
                await bot.send_message(
                    bot_id=self._bot_id,
                    chat_id=support_chat_id,
                    body=f"{sender_name}:\n{clean_body}",
                    file=message.file,
                    wait_callback=False
                )
            else:
                await bot.send_message(
                    bot_id=self._bot_id,
                    chat_id=support_chat_id,
                    body=f"{sender_name}:\n{clean_body or '[Медиа-вложение]'}",
                    wait_callback=False
                )

    async def forward_reply_to_user(self, message: IncomingMessage, bot: Bot, ticket: Ticket) -> None:
        """Пересылает ответ специалиста из треда в личку пользователю."""
        if ticket.status == TicketStatus.CLOSED:
            return

        # Безопасное извлечение фамилии/имени специалиста
        spec_name = _get_display_name(message.sender.username)
        body_text = message.body or "[Медиа-вложение]"

        # logger.warning(message.file.)

        if hasattr(message, "file") and message.file:
            await bot.send_message(
                bot_id=self._bot_id,
                chat_id=ticket.chat_id,
                body=f"{spec_name}:\n{body_text}",
                recipients=[ticket.user_huid],
                file=message.file,
                wait_callback=False
            )
        else:
            await bot.send_message(
                bot_id=self._bot_id,
                chat_id=ticket.chat_id,
                body=f"{spec_name}:\n{body_text}",
                recipients=[ticket.user_huid],
                wait_callback=False
            )

    # ── Жизненный цикл и обратная связь ───────────────────────

    async def request_close_ticket(self, message: IncomingMessage, ticket_id: int, bot: Bot) -> None:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        if not ticket:
            return

        if ticket.status in (TicketStatus.SOLVED, TicketStatus.CLOSED) or ticket.solution_time is not None:
            await bot.answer_message(f"Это обращение #{ticket_id} уже закрыто.")
            return

        bubbles = KeyboardMarkup()
        bubbles.add_button(command=f"/solved 1 {ticket_id}", label="ДА", background_color="#CDFC68")
        bubbles.add_button(command=f"/solved 0 {ticket_id}", label="НЕТ", background_color="#9E6FC3", new_row=False)

        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=ticket.chat_id,
            body="Подскажите, удалось ли решить Ваш запрос?",
            keyboard=bubbles,
            recipients=[ticket.user_huid],
            wait_callback=False
        )
        # ── Запускаем таймер 10 минут ──
        await self.repo.mark_close_request_sent(ticket_id)

        # [AUDIT] Запрос закрытия от оператора
        await get_audit_service().support_action(
            AuditAction.CLOSE_REQUEST_SENT,
            ticket_id=ticket_id,
            support_huid=message.sender.huid,
            support_name=message.sender.username,
            context={"previous_status": ticket.status.value},
        )

    async def handle_problem_solved(self, message: IncomingMessage, ticket_id: int, bot: Bot) -> None:
        await self.repo.reset_close_request(ticket_id)
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        await self.cleanup_support_chat(bot, ticket, message)

        if not ticket:
            return
        await self.repo.set_solution_time(ticket_id)

        # [AUDIT] Пользователь подтвердил решение
        await get_audit_service().user_action(
            AuditAction.TICKET_SOLVED,
            ticket_id=ticket_id,
            user_huid=message.sender.huid,
            user_name=message.sender.username,
            new={"status": "solved"},
        )

    async def handle_problem_not_solved(self, message: IncomingMessage, ticket_id: int, bot: Bot) -> None:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        if not ticket:
            return

        support_service = get_support_service()
        old_specialist_id = ticket.assigned_to

        # 1. Проверяем, онлайн ли старый специалист
        keep_assigned = False
        if old_specialist_id:
            old_specialist = await support_service.get_support_by_id(old_specialist_id)
            if old_specialist and old_specialist.status == SupportStatus.ONLINE:
                keep_assigned = True

        if keep_assigned:
            # специалист тот же — новая сессия
            shift_service = get_shift_service()
            await shift_service.start_ticket_work(
                ticket_id=ticket_id,
                support_id=old_specialist_id,
            )

        # 2. Обновляем тикет в БД (с учетом статуса специалиста)
        updated_ticket = await self.repo.reopen_ticket(ticket_id, keep_assigned=keep_assigned)

        # [AUDIT] Пользователь сообщил, что проблема не решена
        await get_audit_service().user_action(
            AuditAction.TICKET_REOPENED,
            ticket_id=ticket_id,
            user_huid=message.sender.huid,
            user_name=message.sender.username,
            old={"reopen_count": ticket.reopen_count},
            new={"reopen_count": ticket.reopen_count+1},
        )

        if not updated_ticket:
            return

        # 3. Уведомляем пользователя
        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=updated_ticket.chat_id,
            recipients=[updated_ticket.user_huid],
            body="Специалист вернется к решению вашей проблемы. Ожидайте.",
            wait_callback=False
        )

        # 4. Работа с тредом поддержки
        if updated_ticket.support_thread_message_id:
            # Если специалист сменился (был оффлайн), кикаем старого из треда
            if not keep_assigned and old_specialist_id:
                try:
                    await bot.remove_users_from_chat(
                        bot_id=self._bot_id,
                        chat_id=updated_ticket.support_thread_message_id,
                        huids=[old_specialist_id]
                    )
                except Exception as e:
                    logger.error(f"Ошибка при удалении старого специалиста из чата: {e}")

            # Пишем уведомление в тред
            await bot.send_message(
                bot_id=self._bot_id,
                chat_id=updated_ticket.support_thread_message_id,
                body=f"🔄 Пользователь указал, что проблема не решена.\n"
                     f"Возврат на доработку (попытка: {updated_ticket.reopen_count})",
                wait_callback=False
            )

        # 5. Если старый специалист оффлайн, запускаем автоназначение на нового
        if not keep_assigned:
            logger.info(f"Тикет #{updated_ticket.ticket_id} возвращен в очередь с приоритетом (старый спец оффлайн).")

            # auto_assign_ticket подхватит его первым из-за is_priority=True
            assigned = await self.auto_assign_ticket(updated_ticket.ticket_id, bot)

            if not assigned:
                # Если нет свободных специалистов, можно уведомить супервизоров
                logger.warning(
                    f"Тикет #{updated_ticket.ticket_id} возвращен в очередь, "
                    "но нет свободных онлайн-специалистов."
                )

    async def send_rating_request(self, message: IncomingMessage, ticket_id: int, bot: Bot) -> None:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        if not ticket:
            return

        bubbles = KeyboardMarkup()
        bubbles.add_button(
            command=f"/rating 1 {ticket_id}",
            label="1",
            new_row=False,
            background_color='#ff4747d1'
        )
        bubbles.add_button(
            command=f"/rating 2 {ticket_id}",
            label="2",
            new_row=False,
            background_color='#ffaf47d0'
        )
        bubbles.add_button(
            command=f"/rating 3 {ticket_id}",
            label="3",
            new_row=False,
            background_color='#ffff47d0'
        )
        bubbles.add_button(
            command=f"/rating 4 {ticket_id}",
            label="4",
            new_row=False,
            background_color='#bbff47d0'
        )
        bubbles.add_button(
            command=f"/rating 5 {ticket_id}",
            label="5",
            new_row=False,
            background_color='#59ff47d0'
        )
        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=ticket.chat_id,
            body="Оцените удовлетворенность текущим решением от 1 до 5",
            keyboard=bubbles,
            recipients=[ticket.user_huid],
            wait_callback=False
        )

    async def handle_rating(self, message: IncomingMessage, ticket_id: int, rating: int, bot: Bot) -> None:
        await self.repo.reset_close_request(ticket_id)
        await self.repo.set_rating(ticket_id, rating)

        bubbles = KeyboardMarkup()
        # bubbles.add_button(command=f"/comment 1 {ticket_id}", label="ДА", background_color="#CDFC68")
        bubbles.add_button(command=f"/comment 0 {ticket_id}", label="НЕТ", background_color="#9E6FC3", new_row=False)

        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=message.chat.id,
            recipients=[message.sender.huid],
            body="Хотите оставить комментарий? Введите текст ",
            keyboard=bubbles,
            wait_callback=False
        )

    async def is_additional_answered(self, ticket_id: int) -> bool:
        """Проверяет, ответил ли пользователь на вопрос о доп. вопросах."""
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        if not ticket or not ticket.metadata_:
            return False
        return ticket.metadata_.get("additional_answered", False)

    async def mark_additional_answered(self, ticket_id: int) -> None:
        """Помечает, что пользователь ответил на вопрос о доп. вопросах."""
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        if not ticket:
            return

        meta = ticket.metadata_ or {}
        if meta.get("additional_answered"):
            return

        meta["additional_answered"] = True
        await self.repo.update_ticket_metadata(ticket_id, meta)

    async def handle_comment_request(self, message: IncomingMessage, ticket_id: int, want_comment: int,
                                     bot: Bot) -> None:
        await self.repo.reset_close_request(ticket_id)
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        if not ticket:
            return

        if want_comment:
            await bot.send_message(
                bot_id=self._bot_id,
                chat_id=ticket.chat_id,
                recipients=[ticket.user_huid],
                body="Пожалуйста, ответным сообщением напишите Ваш комментарий.",
                wait_callback=False
            )
        else:
            await self.repo.set_comment(ticket_id=ticket.ticket_id, comment='.')
            await self.repo.close_ticket(ticket_id=ticket.ticket_id)
            await self.request_additional_quest(bot, ticket)

    async def get_pending_tickets_list(self) -> Sequence[Ticket]:
        return await self.repo.get_pending_tickets()

    async def request_additional_quest(self, bot: Bot, ticket: Ticket) -> None:
        """Запрос доп. вопроса"""
        bubbles = KeyboardMarkup()
        # bubbles.add_button(command=f"/additional 1 {ticket.ticket_id}", label="ДА", background_color="#CDFC68")
        bubbles.add_button(command=f"/additional 0 {ticket.ticket_id}", label="НЕТ", background_color="#9E6FC3",
                           new_row=False)

        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=ticket.chat_id,
            recipients=[ticket.user_huid],
            body="Имеются ли у Вас дополнительные вопросы?",
            keyboard=bubbles,
            wait_callback=False
        )

    async def cleanup_support_chat(self, bot: Bot, ticket: Ticket, message: IncomingMessage) -> None:
        """Закрывает тикет и удаляет всех специалистов из треда поддержки."""
        timer = 5
        #не осталяем пустой комментарий при закрытии

        # await self.repo.close_ticket(ticket.ticket_id)
        # await self.request_additional_quest(bot, ticket=ticket)

        await bot.send_message(
            bot_id=self._bot_id,
            chat_id=ticket.support_thread_message_id,
            body=f"✅ Пользователь подтвердил закрытие.\n⏳ Через {timer} секунд чат будет очищен.",
            wait_callback=False
        )

        await asyncio.sleep(timer)

        try:
            chat_info = await bot.chat_info(bot_id=self._bot_id, chat_id=ticket.support_thread_message_id)
            specialists_to_remove = [
                member.huid for member in chat_info.members
                if member.kind != UserKinds.BOT
            ]

            if specialists_to_remove:
                await bot.remove_users_from_chat(
                    bot_id=self._bot_id,
                    chat_id=ticket.support_thread_message_id,
                    huids=specialists_to_remove
                )
        except Exception as e:
            logger.error(f"[SERVICE] Ошибка при очистке чата #{ticket.support_thread_message_id}: {e}")

    async def set_comment(self, ticket_id: int, comment: str) -> None:
        await self.repo.set_comment(ticket_id=ticket_id, comment=comment)

    async def save_comment(self, ticket_id: int, comment: str) -> None:
        await self.set_comment(ticket_id, comment)
        await self.repo.close_ticket(ticket_id)

        # [WORK] Закрываем сессию
        shift_service = get_shift_service()
        await shift_service.end_ticket_work(
            ticket_id=ticket_id, reason=WorkSessionEndReason.CLOSED.value,
        )

        # [AUDIT] Комментарий от пользователя  авто-закрытие
        await get_audit_service().system_action(
            AuditAction.COMMENT_RECEIVED,
            ticket_id=ticket_id,
            new={"status": "closed"},
            context={"comment_len": len(comment)},
        )
    # ── Вспомогательные методы ────────────────────────────────

    async def get_open_tickets_list(self) -> Sequence[Ticket]:
        return await self.repo.get_new_and_open_tickets()

    async def get_in_work_tickets_list(self) -> Sequence[Ticket]:
        return await self.repo.get_in_work_tickets()

    async def _format_ticket_history(self, ticket: Ticket) -> str:
        history = await self.repo.get_message_body_from_ticket(ticket_id=ticket.ticket_id)
        return history or "История пуста"

    async def is_feedback_completed(self, ticket_id: int) -> bool:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        return bool(ticket and ticket.rating is not None)

    async def can_submit_rating(self, ticket_id: int) -> bool:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        return bool(ticket and ticket.status == TicketStatus.SOLVED and ticket.rating is None)

    async def can_submit_comment(self, ticket_id: int) -> bool:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        return bool(ticket and ticket.rating is not None and ticket.comment is None)

    async def is_ticket_closed(self, ticket_id: int) -> bool:
        ticket = await self.repo.get_ticket_by_id(ticket_id)
        return bool(ticket and ticket.status == TicketStatus.CLOSED)

    async def mark_operator_reply(self, ticket_id: int) -> None:
        """
        Оператор ответил пользователю.
        Запускаем ожидание ответа пользователя.
        """
        await self.repo.mark_operator_message(ticket_id)

    async def mark_user_reply(self, ticket_id: int) -> None:
        """
        Пользователь ответил.
        Сбрасываем напоминание и ожидание.
        """
        await self.repo.mark_user_message(ticket_id)

    async def reactivate_pending_ticket(self, ticket_id: int, bot: Bot) -> None:
        """
        Пользователь ответил после перевода тикета в PENDING.
        Возвращаем тикет в очередь с приоритетом.
        """
        reactivated = await self.repo.reactivate_pending_ticket(ticket_id)
        if not reactivated:
            logger.info(
                f"Попытка реактивации тикета #{ticket_id} отклонена: "
                "тикет уже изменил статус"
            )
            return
        # ticket = await self.repo.get_ticket_by_id(ticket_id=ticket_id)
        logger.info(f"Тикет #{ticket_id} возвращен из PENDING в очередь с приоритетом")

        # [AUDIT] Реактивация (актор — SYSTEM; пользователь тут неявно)
        await get_audit_service().system_action(
            AuditAction.TICKET_REACTIVATED,
            ticket_id=ticket_id,
            old={"status": "pending"},
            new={"status": "new", "is_priority": True},
        )

        # Пытаемся назначить сразу.
        # Если не получится — тикет останется в очереди с приоритетом.
        await self.auto_assign_ticket(ticket_id, bot)

    async def process_user_response_timeouts(self, bot: Bot) -> int:
        """
        Фоновая проверка:
        1. отправить напоминание через 5 минут после ответа оператора;
        2. перевести в PENDING, если после напоминания прошло еще 5 минут;
        3. закрыть тикет, если он в PENDING уже 2 часа.
        """
        processed = 0

        # 1. Напоминания
        reminder_candidates = await self.repo.get_reminder_candidates(
            UserResponseConfig.REMINDER_AFTER
        )

        for ticket in reminder_candidates:
            marked = await self.repo.try_mark_reminder_sent(
                ticket_id=ticket.ticket_id,
                timeout=UserResponseConfig.REMINDER_AFTER,
            )
            if not marked:
                continue

            try:
                await bot.send_message(
                    bot_id=uuid.UUID(BotConfig.BOT_ID),
                    chat_id=ticket.chat_id,
                    body="Уточните, пожалуйста, актуален ли еще Ваш вопрос?",
                    wait_callback=False
                )
                await bot.send_message(
                    bot_id=uuid.UUID(BotConfig.BOT_ID),
                    chat_id=ticket.support_thread_message_id,
                    body=(
                        f"Отправлено напоминание по тикету #{ticket.ticket_id}"
                    ),
                    wait_callback=False
                )
                processed += 1

                logger.info(f"Отправлено напоминание по тикету #{ticket.ticket_id}")

                # [AUDIT] Напоминание отправлено
                await get_audit_service().system_action(
                    AuditAction.REMINDER_SENT,
                    ticket_id=ticket.ticket_id,
                    context={"task": "user_response_monitor_loop"},
                )
            except Exception as exc:
                logger.exception(
                    f"Не удалось отправить напоминание по тикету #{ticket.ticket_id}: {exc}"
                )
                await self.repo.reset_reminder_sent(ticket.ticket_id)

        # 2. Перевод в PENDING
        pending_candidates = await self.repo.get_pending_candidates(
            UserResponseConfig.PENDING_AFTER_REMINDER
        )

        for ticket in pending_candidates:
            previous_support_id = ticket.assigned_to

            #удаляем специалиста из чата
            await bot.remove_users_from_chat(
                bot_id=self._bot_id,
                chat_id=ticket.support_thread_message_id,
                huids=[ticket.assigned_to, ]
            )
            moved = await self.repo.try_move_to_pending(
                ticket_id=ticket.ticket_id,
                timeout=UserResponseConfig.PENDING_AFTER_REMINDER,
            )

            if moved:
                shift_service = get_shift_service()
                await shift_service.end_ticket_work(
                    ticket_id=ticket.ticket_id,
                    reason=WorkSessionEndReason.PENDING.value,
                )

            if not moved:
                continue

            logger.info(
                f"Тикет #{ticket.ticket_id} переведен в PENDING "
                f"из-за отсутствия ответа пользователя"
            )
            # [AUDIT] Перевод в PENDING (1010 мин)
            await get_audit_service().system_action(
                AuditAction.TICKET_MOVED_TO_PENDING,
                ticket_id=ticket.ticket_id,
                old={
                    "status": "in_work",
                    "assigned_to": str(previous_support_id) if previous_support_id else None,
                },
                new={"status": "pending"},
                context={
                    "reason": "user_inactive_after_reminder",
                    "task": "user_response_monitor_loop",
                },
            )

            await bot.send_message(
                bot_id=uuid.UUID(BotConfig.BOT_ID),
                chat_id=ticket.support_thread_message_id,
                body=(
                    f"⏸ Тикет #{ticket.ticket_id} переведен в статус PENDING: "
                    "пользователь не ответил на сообщение оператора."
                ),
                wait_callback=False
            )
            # Освободившемуся специалисту можно назначить следующий тикет.
            if previous_support_id:
                await self.process_queue(bot)

            processed += 1

        # 3. Закрытие PENDING через 2 часа
        expired_pending = await self.repo.get_expired_pending_candidates(
            UserResponseConfig.CLOSE_PENDING_AFTER
        )

        for ticket in expired_pending:
            closed = await self.repo.try_close_expired_pending(
                ticket_id=ticket.ticket_id,
                timeout=UserResponseConfig.CLOSE_PENDING_AFTER,
            )

            if closed:
                shift_service = get_shift_service()
                await shift_service.end_ticket_work(
                    ticket_id=ticket.ticket_id,
                    reason=WorkSessionEndReason.AUTO_CLOSED.value,
                )

            if not closed:
                continue

            first_name = self._get_first_name(ticket.user_name)

            close_message = (
                f"{first_name}, поскольку Вы не ответили на предыдущее сообщение, "
                "Я вынужден закрыть это обращение. Если проблема всё ещё актуальна "
                "или возникнут новые вопросы, просто напишите нам — чат откроется "
                "заново, и мы с радостью поможем!"
            )
            await bot.send_message(
                bot_id=uuid.UUID(BotConfig.BOT_ID),
                chat_id=ticket.chat_id,
                body=close_message,
                wait_callback=False
            )

            await bot.send_message(
                bot_id=uuid.UUID(BotConfig.BOT_ID),
                chat_id=ticket.support_thread_message_id,
                body=(
                    f"🏁 Тикет #{ticket.ticket_id} автоматически закрыт: "
                    "пользователь не ответил в течение 2 часов."
                ),
                wait_callback=False
            )
            # [AUDIT] Автозакрытие после 2 часов в PENDING
            await get_audit_service().system_action(
                AuditAction.TICKET_AUTO_CLOSED_PENDING,
                ticket_id=ticket.ticket_id,
                old={"status": "pending"},
                new={"status": "closed"},
                context={
                    "reason": "user_inactive_2h",
                    "task": "user_response_monitor_loop",
                },
            )
            processed += 1
        # 4. Таймаут 10 минут после запроса закрытия
        close_request_expired = await self.repo.get_close_request_expired(
            UserResponseConfig.CLOSE_REQUEST_TIMEOUT
        ) or []

        for ticket in close_request_expired:
            previous_support_id = ticket.assigned_to

            # Удаляем специалиста из чата
            await bot.remove_users_from_chat(
                bot_id=self._bot_id,
                chat_id=ticket.support_thread_message_id,
                huids=[ticket.assigned_to]
            )

            moved = await self.repo.try_move_to_pending_from_close_request(
                ticket_id=ticket.ticket_id,
                timeout=UserResponseConfig.CLOSE_REQUEST_TIMEOUT,
            )

            if moved:
                shift_service = get_shift_service()
                await shift_service.end_ticket_work(
                    ticket_id=ticket.ticket_id,
                    reason=WorkSessionEndReason.PENDING.value,
                )

            if not moved:
                continue

            logger.info(
                f"Тикет #{ticket.ticket_id} переведен в PENDING "
                f"из-за отсутствия ответа на запрос закрытия (10 мин)"
            )

            await bot.send_message(
                bot_id=uuid.UUID(BotConfig.BOT_ID),
                chat_id=ticket.support_thread_message_id,
                body=(
                    f"⏸ Тикет #{ticket.ticket_id} переведен в статус PENDING: "
                    "пользователь не ответил на запрос о закрытии в течение 10 минут."
                ),
                wait_callback=False
            )

            # [AUDIT] Перевод в PENDING из-за молчания на запрос закрытия
            await get_audit_service().system_action(
                AuditAction.TICKET_MOVED_TO_PENDING,
                ticket_id=ticket.ticket_id,
                old={
                    "status": "in_work",
                    "assigned_to": str(previous_support_id) if previous_support_id else None,
                },
                new={"status": "pending"},
                context={
                    "reason": "user_ignored_close_request_10min",
                    "task": "user_response_monitor_loop",
                },
            )

            if previous_support_id:
                await self.process_queue(bot)

            processed += 1

        return processed

    @staticmethod
    def _get_first_name(full_name: Optional[str]) -> str:
        """
        Возвращает имя для обращения.

        Если у вас в user_name хранится формат 'Иванов Иван',
        то лучше использовать parts[1].
        Если 'Иван Иванов' — parts[0].

        В вашем тексте ожидается обращение 'Вероника',
        поэтому по умолчанию берем первый элемент.
        """
        if not full_name:
            return "Коллега"

        parts = full_name.split()
        if not parts:
            return "Коллега"

        return parts[1]
