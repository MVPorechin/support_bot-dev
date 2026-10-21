import logging
import uuid
import datetime as dt
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence, List, Dict, Any
from zoneinfo import ZoneInfo

from certifi import where
from sqlalchemy import desc, func, select, update, or_
from sqlalchemy.orm import attributes

from db.sqlalchemy import session
from db.tickets.models import (
    Support,
    SupportStatus,
    Ticket,
    TicketStatus,
    TicketTransfer,
    Supervisor,
    SupportRegistrationRequest,
    RegistrationRequestStatus,
    SupervisorInvite,
    Shift,
    TicketWorkSession,
    TicketPriority
)


logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")



class TicketRepo:
    """Репозиторий для работы с тикетами (SQLAlchemy 2.0 Async ORM)."""

    async def create_or_update(
            self,
            user_huid: uuid.UUID,
            message_body: str,
            user_name: str,
            chat_id: uuid.UUID,
            role: str
    ) -> Ticket:
        """Создаёт новый тикет или дописывает сообщение в активный."""
        dict_role = {
            'user': "Пользователь",
            'support': "Саппорт"
        }

        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(Ticket.user_huid == user_huid, Ticket.status != TicketStatus.CLOSED)
                .order_by(desc(Ticket.created_at))
                .limit(1)
            )
            result = await conn.execute(stmt)
            existing_ticket = result.scalars().first()

            if existing_ticket:
                if message_body:
                    new_chunk = f"\n{dict_role[role]}: {message_body}"
                    existing_ticket.message_body = (existing_ticket.message_body or "") + new_chunk
                existing_ticket.updated_at = datetime.now(MSK)
                return existing_ticket

            ticket = Ticket(
                user_huid=user_huid,
                user_name=user_name,
                chat_id=chat_id,
                message_body=f"{dict_role[role]}: {message_body}" if message_body else "",
            )
            conn.add(ticket)
            await conn.flush()
            return ticket

    async def get_pending_tickets(self) -> Sequence[Ticket]:
        async with session.begin() as conn:
            stmt = select(Ticket).where(Ticket.status == TicketStatus.PENDING).order_by(desc(Ticket.pending_since))
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def mark_close_request_sent(self, ticket_id: int) -> None:
        """Фиксирует время отправки запроса на закрытие и сбрасывает таймеры обычного диалога."""
        stmt = (
            update(Ticket)
            .where(Ticket.ticket_id == ticket_id)
            .values(
                close_request_sent_at=self._utcnow(),
                first_reminder_sent_at=None,  # <--- Сбрасываем, чтобы не было гонки
                last_operator_message_at=None  # <--- Сбрасываем
            )
        )
        async with session.begin() as conn:
            await conn.execute(stmt)

    async def reset_close_request(self, ticket_id: int) -> None:
        """Сбрасывает таймер запроса закрытия (при любом ответе пользователя)."""
        stmt = (
            update(Ticket)
            .where(Ticket.ticket_id == ticket_id)
            .values(close_request_sent_at=None)
        )
        async with session.begin() as conn:
            await conn.execute(stmt)

    async def get_tickets_by_support_name(
            self, name_part: str, limit: int = 20,
    ) -> Sequence[Ticket]:
        """
        Тикеты, назначенные на специалистов, чьё ФИО содержит подстроку.
        Поиск case-insensitive: «иванов» найдёт «Иванов Иван Иванович».
        """
        pattern = f"%{name_part.strip()}%"
        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .join(Support, Ticket.assigned_to == Support.support_id)
                .where(Support.full_name.ilike(pattern))
                .order_by(desc(Ticket.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_tickets_by_user_name(
            self, name_part: str, limit: int = 20,
    ) -> Sequence[Ticket]:
        """
        Тикеты пользователя по подстроке ФИО.
        Использует tickets.user_name (кэшируется при создании тикета).
        """
        pattern = f"%{name_part.strip()}%"
        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(Ticket.user_name.ilike(pattern))
                .order_by(desc(Ticket.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_tickets_by_period(
            self,
            date_from: datetime,
            date_to: datetime,
            limit: int = 20,
    ) -> Sequence[Ticket]:
        """
        Тикеты, созданные в интервале [date_from, date_to].
        """
        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(
                    Ticket.created_at >= date_from,
                    Ticket.created_at <= date_to,
                )
                .order_by(desc(Ticket.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()


    async def get_close_request_expired(self, timeout: timedelta) -> Sequence[Ticket]:
        """Тикеты, где запрос закрытия отправлен, но ответа нет больше timeout."""
        cutoff = self._utcnow() - timeout
        stmt = (
            select(Ticket)
            .where(
                Ticket.status.in_([TicketStatus.IN_WORK, TicketStatus.SOLVED]),
                Ticket.close_request_sent_at.is_not(None),
                Ticket.close_request_sent_at <= cutoff,
            )
            .limit(200)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def try_move_to_pending_from_close_request(self, ticket_id: int, timeout: timedelta) -> bool:
        """Атомарно переводит тикет в PENDING из-за игнорирования запроса закрытия."""
        now = self._utcnow()
        cutoff = now - timeout
        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status.in_([TicketStatus.IN_WORK, TicketStatus.SOLVED]),
                Ticket.close_request_sent_at.is_not(None),
                Ticket.close_request_sent_at <= cutoff,
            )
            .values(
                status=TicketStatus.PENDING,
                pending_since=now,
                assigned_to=None,
                close_request_sent_at=None,
                is_priority=False,
            )
            .returning(Ticket.ticket_id)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None

    async def get_all_tickets_with_specialist(self) -> List[Dict[str, Any]]:
        """
        Получает все тикеты с информацией о назначенных специалистах.
        Используется для экспорта данных.
        """
        async with session.begin() as conn:
            stmt = (
                select(
                    Ticket.ticket_id,
                    Ticket.user_name,
                    Ticket.user_huid,
                    Ticket.status,
                    Ticket.created_at,
                    Ticket.updated_at,
                    Support.full_name.label('assigned_to_name'),
                    Support.support_id.label('assigned_to_huid'),
                    Ticket.taken_at,
                    Ticket.solution_time,
                    Ticket.rating,
                    Ticket.comment,
                    Ticket.reopen_count,
                    Ticket.closed_at,
                    Ticket.sla_deadline,
                    Ticket.sla_breached,
                )
                .outerjoin(Support, Ticket.assigned_to == Support.support_id)
                .order_by(desc(Ticket.created_at))
            )

            result = await conn.execute(stmt)
            rows = result.fetchall()

            # Конвертируем Row объекты в словари
            return [dict(row._mapping) for row in rows]

    async def get_all_tickets_with_messages(self) -> List[Dict[str, Any]]:
        """
        Получает все тикеты с полной перепиской.
        Используется для экспорта истории диалогов.
        """
        async with session.begin() as conn:
            stmt = (
                select(
                    Ticket.ticket_id,
                    Ticket.user_name,
                    Ticket.user_huid,
                    Ticket.status,
                    Ticket.created_at,
                    Support.full_name.label('assigned_to_name'),
                    Ticket.message_body,
                )
                .outerjoin(Support, Ticket.assigned_to == Support.support_id)
                .where(Ticket.message_body.isnot(None))  # Только тикеты с перепиской
                .order_by(desc(Ticket.created_at))
            )

            result = await conn.execute(stmt)
            rows = result.fetchall()

            # Конвертируем Row объекты в словари
            return [dict(row._mapping) for row in rows]

    async def resolve_pending_ticket(self, ticket_id: int) -> bool:
        """
        Переводит PENDING тикет в SOLVED без реактивации.
        Используется, когда пользователь подтвердил решение проблемы.
        """
        now = self._utcnow()
        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status == TicketStatus.PENDING,
            )
            .values(
                status=TicketStatus.SOLVED,
                solution_time=now,
                updated_at=now,
                pending_since=None,
                close_request_sent_at=None,
                first_reminder_sent_at=None,
                last_operator_message_at=None,
            )
            .returning(Ticket.ticket_id)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None

    async def update_ticket_metadata(self, ticket_id: int, metadata: dict) -> None:
        """Обновляет поле metadata (JSONB) у тикета."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.metadata_ = metadata
                # flag_modified обязателен, чтобы SQLAlchemy заметил изменение словаря
                attributes.flag_modified(ticket, "metadata_")

    async def set_support_chat_id(self, ticket_id: int, message_id: uuid.UUID) -> None:
        """Сохраняет ID чата поддержки."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.support_thread_message_id = message_id

    async def get_thread_id(self, ticket_id: int) -> Optional[uuid.UUID]:
        """Получает ID треда из БД."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            return ticket.support_thread_message_id if ticket else None

    async def get_thread_by_user(self, user_huid: uuid.UUID) -> Optional[uuid.UUID]:
        """Получает thread_id по пользователю."""
        stmt = (
            select(Ticket.support_thread_message_id)
            .where(
                Ticket.user_huid == user_huid,
                Ticket.support_thread_message_id.is_not(None)
            )
            .order_by(
                Ticket.is_priority.desc(),  # сначала приоритетные
                Ticket.created_at.asc(),  #  берем самый свежий тред
            )
            .limit(1)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalar_one_or_none()

    async def get_open_ticket_by_user(self, user_huid: uuid.UUID) -> Optional[Ticket]:
        """Возвращает последнее открытое обращение пользователя или None."""
        async with session.begin() as conn:
            stmt = select(Ticket).where(
                Ticket.user_huid == user_huid,
                Ticket.status != TicketStatus.CLOSED
                   ).order_by(
                Ticket.is_priority.desc(),
                Ticket.created_at.desc()
            ).limit(1)

            result = await conn.execute(stmt)
            return result.scalars().first()

    async def get_new_and_open_tickets(self) -> Sequence[Ticket]:
        """Список заявок в статусах NEW или OPEN упорядоченных по дате создания."""
        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(Ticket.status.in_([TicketStatus.NEW, TicketStatus.OPEN]))
                .order_by(desc(Ticket.created_at))
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_in_work_tickets(self) -> Sequence[Ticket]:
        """Список заявок в статусах IN_WORK упорядоченных по дате создания."""
        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(Ticket.status == TicketStatus.IN_WORK)
                .order_by(desc(Ticket.created_at))
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def assign_ticket(self, ticket_id: int, specialist_huid: uuid.UUID) -> Optional[Ticket]:
        """Назначить заявку на специалиста: сменить статус на IN_WORK."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if not ticket or ticket.status not in (TicketStatus.NEW, TicketStatus.OPEN):
                return None

            ticket.status = TicketStatus.IN_WORK
            ticket.assigned_to = specialist_huid
            ticket.taken_at = datetime.now(MSK)
            ticket.updated_at = datetime.now(MSK)
            ticket.is_priority = False
            return ticket

    async def reassign_ticket(self, ticket_id: int, specialist_huid: uuid.UUID) -> Optional[Ticket]:
        """Переназначить заявку с одного специалиста на другого."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if not ticket:
                return None

            ticket.status = TicketStatus.IN_WORK
            ticket.assigned_to = specialist_huid
            ticket.taken_at = datetime.now(MSK)
            ticket.updated_at = datetime.now(MSK)
            ticket.is_priority = False  # тикет взят в работу, приоритет больше не нужен
            return ticket

    async def get_ticket_by_chat_id(self, chat_id: uuid.UUID) -> Optional[Ticket]:
        """Ищет тикет по ID чата с пользователем."""
        async with session.begin() as conn:
            stmt = select(Ticket).where(Ticket.chat_id == chat_id).limit(1)
            result = await conn.execute(stmt)
            return result.scalars().first()

    async def reopen_ticket(self, ticket_id: int, keep_assigned: bool) -> Optional[Ticket]:
        """
        Переоткрывает тикет.
        Если keep_assigned=True, оставляет на текущем специалисте (статус IN_WORK).
        Если keep_assigned=False, сбрасывает специалиста, ставит приоритет и переводит в NEW.
        """
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if not ticket:
                return None

            ticket.reopen_count += 1
            ticket.updated_at = datetime.now(MSK)

            # Сбрасываем таймеры закрытия и PENDING, если они были активны
            ticket.close_request_sent_at = None
            ticket.first_reminder_sent_at = None
            ticket.pending_since = None

            if keep_assigned:
                # Специалист онлайн — оставляем тикет на нем
                ticket.status = TicketStatus.IN_WORK
            else:
                # Специалист оффлайн — возвращаем в очередь с приоритетом
                ticket.status = TicketStatus.NEW
                ticket.assigned_to = None
                ticket.is_priority = True  # <--- КЛЮЧЕВОЙ МОМЕНТ: тикет встанет первым

            return ticket

    async def get_ticket_by_thread_id(self, message_id: uuid.UUID | str) -> Optional[Ticket]:
        """Получаем тикет по ID треда поддержки."""
        if isinstance(message_id, str):
            message_id = uuid.UUID(message_id)

        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(Ticket.support_thread_message_id == message_id, Ticket.status != TicketStatus.CLOSED)
                .limit(1)
            )
            result = await conn.execute(stmt)
            ticket = result.scalars().first()

            if ticket:
                logger.info(f"[REPO] Найден тикет #{ticket.ticket_id} по thread_id={message_id}")
            else:
                logger.warning(f"[REPO] Тикет не найден по thread_id={message_id} или это персональный чат")

            return ticket

    async def set_solution_time(self, ticket_id: int) -> None:
        """Фиксирует время решения и переводит в SOLVED."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.status = TicketStatus.SOLVED
                ticket.solution_time = datetime.now(MSK)
                ticket.updated_at = datetime.now(MSK)

    async def get_ticket_by_id(self, ticket_id: int) -> Optional[Ticket]:
        """Возвращает тикет по его внутреннему ID."""
        async with session.begin() as conn:
            return await conn.get(Ticket, ticket_id)

    async def increment_reopen_count(self, ticket_id: int) -> None:
        """Увеличиваем счетчик возвращаемого обращения."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.reopen_count += 1
                ticket.status = TicketStatus.IN_WORK
                ticket.updated_at = datetime.now(MSK)

    async def set_rating(self, ticket_id: int, rating: int) -> None:
        """Оценка от пользователя за тикет."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.rating = rating
                ticket.updated_at = datetime.now(MSK)

    async def set_comment(self, ticket_id: int, comment: str) -> None:
        """Храним комментарий от пользователя."""
        async with session.begin() as conn:
            stmt = select(Ticket).where(Ticket.ticket_id == ticket_id)
            result = await conn.execute(stmt)
            existing_ticket = result.scalars().first()

            if existing_ticket:
                existing_ticket.comment = (existing_ticket.comment or "") + comment
                existing_ticket.closed_at = datetime.now(MSK)
                existing_ticket.updated_at = datetime.now(MSK)
                await conn.flush()

    # ── SLA методы ───────────────────────────────────────

    async def set_sla_deadline(self, ticket_id: int, deadline: datetime) -> None:
        """Устанавливает деадлайн SLA для тикета."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.sla_deadline = deadline

    async def reactivate_pending_to_work(
        self,
        ticket_id: int,
        specialist_huid: uuid.UUID,
    ) -> Optional[Ticket]:
        """
        PENDING → IN_WORK с прямым назначением на специалиста.
        Сбрасывает все таймеры PENDING и close_request.
        Возвращает None, если тикет уже не в PENDING (защита от race).
        """
        now = self._utcnow()
        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status == TicketStatus.PENDING,
            )
            .values(
                status=TicketStatus.IN_WORK,
                assigned_to=specialist_huid,
                taken_at=now,
                updated_at=now,
                # Сброс всех таймеров жизненного цикла
                pending_since=None,
                close_request_sent_at=None,
                first_reminder_sent_at=None,
                last_operator_message_at=None,
                # Приоритет не нужен — тикет уже назначен
                is_priority=False,
            )
            .returning(Ticket.ticket_id)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            if result.first() is None:
                return None
            return await conn.get(Ticket, ticket_id)

    async def get_pending_tickets(self, limit: int = 200) -> Sequence[Ticket]:
        """
        Список тикетов в PENDING, отсортированный по давности.
        Самые старые — сверху (они «сгорают» первыми).
        """
        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(Ticket.status == TicketStatus.PENDING)
                .order_by(Ticket.pending_since.asc().nullslast())
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_tickets_approaching_sla(self, hours_left: int) -> Sequence[Ticket]:
        """Возвращает тикеты, у которых до SLA осталось ≤ hours_left часов."""
        now = datetime.now(MSK)
        threshold = now + timedelta(hours=hours_left)

        async with session.begin() as conn:
            stmt = (
                select(Ticket)
                .where(
                    Ticket.status.in_([TicketStatus.NEW, TicketStatus.OPEN, TicketStatus.IN_WORK]),
                    Ticket.sla_deadline.isnot(None),
                    Ticket.sla_deadline <= threshold,
                    Ticket.sla_breached.is_(False),
                )
                .order_by(Ticket.sla_deadline)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def mark_sla_breached(self, ticket_id: int) -> None:
        """Помечает тикет как нарушивший SLA."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.sla_breached = True

    async def update_sla_checkpoint(self, ticket_id: int, checkpoint_name: str) -> None:
        """Сохраняет пройденную контрольную точку в metadata тикета (JSONB)."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if not ticket:
                return

            meta = ticket.metadata_ or {}
            sla_log: list = meta.setdefault("sla_checkpoints", [])
            sla_log.append({
                "checkpoint": checkpoint_name,
                "at": datetime.now(MSK).isoformat(),
            })

            # Переприсваивание и flag_modified нужны, чтобы SQLAlchemy
            # заметил in-place мутацию словаря и отправил UPDATE в БД
            ticket.metadata_ = meta
            attributes.flag_modified(ticket, "metadata_")

    async def close_ticket(self, ticket_id: int) -> None:
        """Закрываем тикет."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            if ticket:
                ticket.status = TicketStatus.CLOSED
                ticket.closed_at = datetime.now(MSK)
                ticket.updated_at = datetime.now(MSK)

    async def get_expired_solved_tickets(self) -> Sequence[Ticket]:
        """Возвращаем тикеты в статусе SOLVED у которых solution_time старше 2 часов."""
        deadline = datetime.now(MSK) - timedelta(hours=2)
        async with session.begin() as conn:
            stmt = select(Ticket).where(Ticket.status == TicketStatus.SOLVED, Ticket.solution_time <= deadline)
            result = await conn.execute(stmt)
            return result.scalars().all()

    # Alias для обратной совместимости (в оригинале была опечатка exprired)
    get_exprired_solved_tickets = get_expired_solved_tickets

    async def get_next_from_queue(self) -> Optional[Ticket]:
        """Получает самый старый тикет из очереди (NEW)."""
        async with session.begin() as conn:
            stmt = select(Ticket).where(Ticket.status.in_(
                [TicketStatus.NEW, TicketStatus.OPEN])
            ).order_by(
                Ticket.is_priority.desc(), Ticket.created_at.asc()
            ).limit(1)
            result = await conn.execute(stmt)
            return result.scalars().first()

    async def get_specialist_load(self, specialist_huid: uuid.UUID) -> int:
        """Считает, сколько тикетов сейчас в работе у специалиста."""
        async with session.begin() as conn:
            stmt = select(func.count(Ticket.ticket_id)).where(
                Ticket.assigned_to == specialist_huid, Ticket.status == TicketStatus.IN_WORK
            )
            result = await conn.execute(stmt)
            return result.scalar_one()

    async def get_tickets_by_specialist(self, assigned_to: uuid.UUID) -> Sequence[Ticket]:
        """Возвращает список тикетов назначенных на специалиста и не в статусе CLOSED."""
        async with session.begin() as conn:
            stmt = select(Ticket).where(
                Ticket.assigned_to == assigned_to,
                Ticket.status != TicketStatus.CLOSED
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_message_body_from_ticket(self, ticket_id: int) -> Optional[str]:
        """Получаем все сообщения от пользователей поступившие в рамках тикета."""
        async with session.begin() as conn:
            ticket = await conn.get(Ticket, ticket_id)
            return ticket.message_body if ticket else None

    async def get_all_new_tickets(self) -> Sequence[Ticket]:
        """Возвращает все тикеты в статусе NEW (в очереди)."""
        async with session.begin() as conn:
            stmt = select(Ticket).where(Ticket.status.in_(
                [TicketStatus.NEW, TicketStatus.OPEN])
            ).order_by(
                Ticket.is_priority.desc(),Ticket.created_at.asc()
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    @staticmethod
    def _utcnow() -> dt.datetime:
        return dt.datetime.now(MSK)

    async def mark_operator_message(self, ticket_id: int) -> None:
        """
        Вызывается, когда оператор ответил пользователю.
        Запускает новый отсчет ожидания ответа пользователя.
        """
        stmt = (
            update(Ticket)
            .where(Ticket.ticket_id == ticket_id)
            .values(
                last_operator_message_at=self._utcnow(),
                first_reminder_sent_at=None,
                pending_since=None,
                is_priority=False,
            )
        )

        async with session.begin() as conn:
            await conn.execute(stmt)
            await conn.commit()

    async def mark_user_message(self, ticket_id: int) -> None:
        """
        Вызывается, когда пользователь ответил в тикете.
        Сбрасывает напоминание и ожидание.
        """
        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status == TicketStatus.IN_WORK,
            )
            .values(
                last_user_message_at=self._utcnow(),
                first_reminder_sent_at=None,
                pending_since=None,
            )
        )

        async with session.begin() as conn:
            await conn.execute(stmt)
            await conn.commit()

    async def get_reminder_candidates(self, timeout: timedelta) -> Sequence[Ticket]:
        """
        Тикеты, где оператор ответил, пользователь не ответил,
        прошло больше timeout и напоминание еще не отправлялось.
        """
        cutoff = self._utcnow() - timeout
        stmt = (
            select(Ticket)
            .where(
                Ticket.status == TicketStatus.IN_WORK,
                Ticket.first_reminder_sent_at.is_(None),
                Ticket.close_request_sent_at.is_(None),  # <--- ДОБАВИТЬ
                Ticket.last_operator_message_at.is_not(None),
                Ticket.last_operator_message_at <= cutoff,
                or_(
                    Ticket.last_user_message_at.is_(None),
                    Ticket.last_user_message_at < Ticket.last_operator_message_at,
                ),
            )
            .limit(200)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def try_mark_reminder_sent(self, ticket_id: int, timeout: timedelta) -> bool:
        """
        Атомарно помечает напоминание отправленным.
        Возвращает False, если пользователь уже ответил или статус изменился.
        """
        now = self._utcnow()
        cutoff = now - timeout
        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status == TicketStatus.IN_WORK,
                Ticket.first_reminder_sent_at.is_(None),
                Ticket.close_request_sent_at.is_(None),  # <--- ДОБАВИТЬ
                Ticket.last_operator_message_at.is_not(None),
                Ticket.last_operator_message_at <= cutoff,
                or_(
                    Ticket.last_user_message_at.is_(None),
                    Ticket.last_user_message_at < Ticket.last_operator_message_at,
                ),
            )
            .values(first_reminder_sent_at=now)
            .returning(Ticket.ticket_id)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None

    async def reset_reminder_sent(self, ticket_id: int) -> None:
        """
        Если не удалось отправить сообщение, возвращаем состояние назад.
        """
        stmt = (
            update(Ticket)
            .where(Ticket.ticket_id == ticket_id)
            .values(first_reminder_sent_at=None)
        )

        async with session.begin() as conn:
            await conn.execute(stmt)
            await conn.commit()

    async def get_pending_candidates(self, timeout: timedelta) -> Sequence[Ticket]:
        """
        Тикеты, где напоминание уже отправлено,
        пользователь не ответил после напоминания,
        прошло еще timeout.
        """
        cutoff = self._utcnow() - timeout
        stmt = (
            select(Ticket)
            .where(
                Ticket.status == TicketStatus.IN_WORK,
                Ticket.first_reminder_sent_at.is_not(None),
                Ticket.close_request_sent_at.is_(None),  # <--- ДОБАВИТЬ
                Ticket.first_reminder_sent_at <= cutoff,
                or_(
                    Ticket.last_user_message_at.is_(None),
                    Ticket.last_user_message_at < Ticket.first_reminder_sent_at,
                ),
            )
            .limit(200)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def try_move_to_pending(self, ticket_id: int, timeout: timedelta) -> bool:
        """
        Атомарно переводит тикет в PENDING и освобождает оператора.
        """
        now = self._utcnow()
        cutoff = now - timeout
        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status == TicketStatus.IN_WORK,
                Ticket.first_reminder_sent_at.is_not(None),
                Ticket.close_request_sent_at.is_(None),  # <--- ДОБАВИТЬ
                Ticket.first_reminder_sent_at <= cutoff,
                or_(
                    Ticket.last_user_message_at.is_(None),
                    Ticket.last_user_message_at < Ticket.first_reminder_sent_at,
                ),
            )
            .values(
                status=TicketStatus.PENDING,
                pending_since=now,
                assigned_to=None,
                first_reminder_sent_at=None,
                last_operator_message_at=None,
                is_priority=False,
            )
            .returning(Ticket.ticket_id)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None

    async def get_expired_pending_candidates(self, timeout: timedelta) -> Sequence[Ticket]:
        """
        Тикеты в PENDING, которые ждут уже больше 2 часов.
        """
        cutoff = self._utcnow() - timeout

        stmt = (
            select(Ticket)
            .where(
                Ticket.status == TicketStatus.PENDING,
                Ticket.pending_since.is_not(None),
                Ticket.pending_since <= cutoff,
                or_(
                    Ticket.last_user_message_at.is_(None),
                    Ticket.last_user_message_at < Ticket.pending_since,
                ),
            )
            .limit(200)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def try_close_expired_pending(self, ticket_id: int, timeout: timedelta) -> bool:
        """
        Атомарно закрывает PENDING-тикет, если пользователь так и не ответил.
        """
        now = self._utcnow()
        cutoff = now - timeout

        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status == TicketStatus.PENDING,
                Ticket.pending_since.is_not(None),
                Ticket.pending_since <= cutoff,
                or_(
                    Ticket.last_user_message_at.is_(None),
                    Ticket.last_user_message_at < Ticket.pending_since,
                ),
            )
            .values(
                status=TicketStatus.CLOSED,
                closed_at=now,
                assigned_to=None,
            )
            .returning(Ticket.ticket_id)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None

    async def reactivate_pending_ticket(self, ticket_id: int) -> bool:
        """
        Возвращает PENDING-тикет в очередь с приоритетом.
        """
        now = self._utcnow()

        stmt = (
            update(Ticket)
            .where(
                Ticket.ticket_id == ticket_id,
                Ticket.status == TicketStatus.PENDING,
            )
            .values(
                status=TicketStatus.NEW,
                assigned_to=None,
                pending_since=None,
                first_reminder_sent_at=None,
                last_operator_message_at=None,
                last_user_message_at=now,
                is_priority=True,
            )
            .returning(Ticket.ticket_id)
        )

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None


class SupportRepo:
    """Репозиторий для работы со специалистами поддержки."""

    async def get_or_create_specialist(self, support_id: uuid.UUID, full_name: str) -> Support:
        """Получает специалиста из БД или создает нового (со статусом OFFLINE)."""
        async with session.begin() as conn:
            specialist = await conn.get(Support, support_id)
            if not specialist:
                specialist = Support(
                    support_id=support_id,
                    full_name=full_name,
                    status=SupportStatus.OFFLINE
                )
                conn.add(specialist)
                await conn.flush()
            return specialist

    async def get_all_online_specialists(self) -> Sequence[Support]:
        """Возвращает список всех специалистов со статусом ONLINE."""
        async with session.begin() as conn:
            stmt = select(Support).where(Support.status == SupportStatus.ONLINE)
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def set_status(self, support_id: uuid.UUID, status: SupportStatus) -> None:
        """Обновляет статус специалиста (ONLINE/OFFLINE)."""
        async with session.begin() as conn:
            specialist = await conn.get(Support, support_id)
            if specialist:
                specialist.status = status

    async def get_by_id(self, support_id: uuid.UUID) -> Optional[Support]:
        """Получить специалиста по UUID."""
        async with session.begin() as conn:
            stmt = select(Support).where(Support.support_id == support_id)
            result = await conn.execute(stmt)
            return result.scalars().first()

    async def get_all(self) -> Sequence[uuid.UUID]:
        """Получить список всех специалистов (только ID)."""
        async with session.begin() as conn:
            stmt = select(Support.support_id)
            result = await conn.execute(stmt)
            return result.scalars().all()

    # ── Заявки на регистрацию ────────────────────────────────

    async def create_registration_request(
        self, support_id: uuid.UUID, full_name: str, chat_id: uuid.UUID
    ) -> Optional[SupportRegistrationRequest]:
        """
        Создаёт pending-заявку. Возвращает None, если уже есть pending —
        это защита от спама (уникальный partial-индекс тоже не даст создать).
        """
        async with session.begin() as conn:
            # Проверяем существующую pending
            stmt = select(SupportRegistrationRequest).where(
                SupportRegistrationRequest.support_id == support_id,
                SupportRegistrationRequest.status == RegistrationRequestStatus.PENDING,
            )
            result = await conn.execute(stmt)
            if result.scalars().first():
                return None

            request = SupportRegistrationRequest(
                support_id=support_id,
                full_name=full_name,
                chat_id=chat_id
            )
            conn.add(request)
            await conn.flush()
            return request

    async def get_registration_request(
        self, request_id: int,
    ) -> Optional[SupportRegistrationRequest]:
        async with session.begin() as conn:
            return await conn.get(SupportRegistrationRequest, request_id)

    async def get_pending_registration_requests(
        self, limit: int = 50,
    ) -> Sequence[SupportRegistrationRequest]:
        async with session.begin() as conn:
            stmt = (
                select(SupportRegistrationRequest)
                .where(SupportRegistrationRequest.status == RegistrationRequestStatus.PENDING)
                .order_by(SupportRegistrationRequest.created_at.asc())
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def approve_registration(
        self, request_id: int, supervisor_huid: uuid.UUID,
    ) -> Optional[SupportRegistrationRequest]:
        """
        Атомарно: pending → approved.
        Возвращает None, если заявка уже обработана (защита от двойного клика).
        """
        now = datetime.now(MSK)
        stmt = (
            update(SupportRegistrationRequest)
            .where(
                SupportRegistrationRequest.id == request_id,
                SupportRegistrationRequest.status == RegistrationRequestStatus.PENDING,
            )
            .values(
                status=RegistrationRequestStatus.APPROVED,
                resolved_at=now,
                resolved_by=supervisor_huid,
            )
            .returning(SupportRegistrationRequest.id)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            if result.first() is None:
                return None
            return await conn.get(SupportRegistrationRequest, request_id)

    async def reject_registration(
        self, request_id: int, supervisor_huid: uuid.UUID, reason: str,
    ) -> Optional[SupportRegistrationRequest]:
        now = datetime.now(MSK)
        stmt = (
            update(SupportRegistrationRequest)
            .where(
                SupportRegistrationRequest.id == request_id,
                SupportRegistrationRequest.status == RegistrationRequestStatus.PENDING,
            )
            .values(
                status=RegistrationRequestStatus.REJECTED,
                resolved_at=now,
                resolved_by=supervisor_huid,
                reject_reason=reason,
            )
            .returning(SupportRegistrationRequest.id)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            if result.first() is None:
                return None
            return await conn.get(SupportRegistrationRequest, request_id)

    # ── chat_id ──────────────────────────────────────────────

    async def set_chat_id(
        self, support_id: uuid.UUID, chat_id: uuid.UUID,
    ) -> bool:
        """
        Устанавливает chat_id специалисту, если он ещё не задан.
        Идемпотентно.
        Возвращает True, если записалось (или уже совпадает),
        False — если специалист не найден или chat_id конфликтует.
        """
        async with session.begin() as conn:
            specialist = await conn.get(Support, support_id)
            if specialist is None:
                return False

            # Уже задан — не перезаписываем
            if specialist.chat_id is not None:
                return specialist.chat_id == chat_id

            specialist.chat_id = chat_id
            return True

    async def get_chat_id(
        self, support_id: uuid.UUID,
    ) -> Optional[uuid.UUID]:
        async with session.begin() as conn:
            specialist = await conn.get(Support, support_id)
            return specialist.chat_id if specialist else None

    async def get_all_with_chat(self) -> Sequence[Support]:
        """
        Возвращает всех специалистов с заполненным chat_id.
        Используется фоновыми задачами для уведомлений.
        """
        async with session.begin() as conn:
            stmt = (
                select(Support)
                .where(Support.chat_id.is_not(None))
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def set_online_with_shift(
        self,
        support_id: uuid.UUID,
        shift_ends_at: Optional[datetime],
    ) -> None:
        """Переводит специалиста в ONLINE с указанием конца смены."""
        async with session.begin() as conn:
            specialist = await conn.get(Support, support_id)
            if specialist:
                specialist.status = SupportStatus.ONLINE
                specialist.shift_ends_at = shift_ends_at

    async def set_offline(
        self,
        support_id: uuid.UUID,
    ) -> None:
        """Переводит специалиста в OFFLINE и снимает таймер смены."""
        async with session.begin() as conn:
            specialist = await conn.get(Support, support_id)
            if specialist:
                specialist.status = SupportStatus.OFFLINE
                specialist.shift_ends_at = None

    async def get_expired_shifts(self) -> Sequence[Support]:
        """
        Специалисты, у которых истёк shift_ends_at,
        но статус всё ещё ONLINE.
        """
        now = datetime.now(MSK)
        async with session.begin() as conn:
            stmt = (
                select(Support)
                .where(
                    Support.status == SupportStatus.ONLINE,
                    Support.shift_ends_at.is_not(None),
                    Support.shift_ends_at <= now,
                )
            )
            result = await conn.execute(stmt)
            return result.scalars().all()
class TicketTransferRepo:
    """Репозиторий для аудита переводов тикетов."""

    async def set(
            self,
            ticket_id: int,
            from_huid: uuid.UUID,
            to_huid: uuid.UUID,
            comment: Optional[str]
    ) -> TicketTransfer:
        """Создает запись о переводе тикета."""
        async with session.begin() as conn:
            ticket_transfer = TicketTransfer(
                ticket_id=ticket_id,
                from_huid=from_huid,
                to_huid=to_huid,
                comment=comment
            )
            conn.add(ticket_transfer)
            await conn.flush()
            return ticket_transfer

    async def get_transfer_history(self, ticket_id: int) -> Sequence[TicketTransfer]:
        """Возвращает историю переводов тикета, отсортированную по времени (новые сверху)."""
        async with session.begin() as conn:
            stmt = (
                select(TicketTransfer)
                .where(TicketTransfer.ticket_id == ticket_id)
                .order_by(desc(TicketTransfer.transferred_at))
            )
            result = await conn.execute(stmt)
            return result.scalars().all()


class SupervisorRepo:
    """Репозиторий для работы с супервизорами."""

    async def get_or_create(self, supervisor_id: uuid.UUID, full_name: str) -> "Supervisor":
        async with session.begin() as conn:
            supervisor = await conn.get(Supervisor, supervisor_id)
            if not supervisor:
                supervisor = Supervisor(
                    supervisor_id=supervisor_id,
                    full_name=full_name,
                )
                conn.add(supervisor)
                await conn.flush()
            return supervisor

    async def get_all_active(self) -> Sequence[uuid.UUID]:
        """Список ID всех активных супервизоров."""
        async with session.begin() as conn:
            stmt = select(Supervisor.supervisor_id).where(Supervisor.is_active.is_(True))
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_by_id(self, supervisor_id: uuid.UUID) -> Optional["Supervisor"]:
        async with session.begin() as conn:
            return await conn.get(Supervisor, supervisor_id)

    async def deactivate(self, supervisor_id: uuid.UUID) -> None:
        async with session.begin() as conn:
            supervisor = await conn.get(Supervisor, supervisor_id)
            if supervisor:
                supervisor.is_active = False

    # ── Приглашения ──────────────────────────────────────────

    async def create_invite(
        self,
        created_by: uuid.UUID,
        ttl_hours: int = 24,
    ) -> SupervisorInvite:
        """Создаёт одноразовое приглашение с TTL."""
        now = datetime.now(MSK)
        invite = SupervisorInvite(
            code=uuid.uuid4(),
            created_by=created_by,
            expires_at=now + timedelta(hours=ttl_hours),
        )
        async with session.begin() as conn:
            conn.add(invite)
            await conn.flush()
            return invite

    async def get_invite_by_code(self, code: uuid.UUID) -> Optional[SupervisorInvite]:
        async with session.begin() as conn:
            stmt = select(SupervisorInvite).where(SupervisorInvite.code == code)
            result = await conn.execute(stmt)
            return result.scalars().first()

    async def use_invite(
        self, code: uuid.UUID, used_by: uuid.UUID,
    ) -> Optional[SupervisorInvite]:
        """
        Атомарно помечает приглашение использованным.
        Возвращает None, если оно уже использовано/отозвано/истекло.
        """
        now = datetime.now(MSK)
        stmt = (
            update(SupervisorInvite)
            .where(
                SupervisorInvite.code == code,
                SupervisorInvite.used_at.is_(None),
                SupervisorInvite.revoked_at.is_(None),
                SupervisorInvite.expires_at > now,
            )
            .values(used_by=used_by, used_at=now)
            .returning(SupervisorInvite.id)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            if result.first() is None:
                return None
            stmt2 = select(SupervisorInvite).where(SupervisorInvite.code == code)
            r2 = await conn.execute(stmt2)
            return r2.scalars().first()

    async def revoke_invite(
        self, code: uuid.UUID, revoked_by: uuid.UUID,
    ) -> bool:
        now = datetime.now(MSK)
        stmt = (
            update(SupervisorInvite)
            .where(
                SupervisorInvite.code == code,
                SupervisorInvite.used_at.is_(None),
                SupervisorInvite.revoked_at.is_(None),
            )
            .values(revoked_by=revoked_by, revoked_at=now)
            .returning(SupervisorInvite.id)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None

    async def get_my_invites(
        self, created_by: uuid.UUID, limit: int = 20,
    ) -> Sequence[SupervisorInvite]:
        async with session.begin() as conn:
            stmt = (
                select(SupervisorInvite)
                .where(SupervisorInvite.created_by == created_by)
                .order_by(desc(SupervisorInvite.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def set_chat_id(
        self, supervisor_id: uuid.UUID, chat_id: uuid.UUID,
    ) -> bool:
        """
        Устанавливает chat_id супервизору, если он ещё не задан.
        Идемпотентно — повторные вызовы с тем же chat_id ничего не делают.
        Если chat_id уже есть и совпадает — возвращает True без записи.
        Если chat_id уже есть и отличается — не перезаписывает (возвращает False).
        """
        async with session.begin() as conn:
            supervisor = await conn.get(Supervisor, supervisor_id)
            if supervisor is None:
                return False

            # Уже задан — не трогаем
            if supervisor.chat_id is not None:
                return supervisor.chat_id == chat_id

            supervisor.chat_id = chat_id
            return True

    async def get_chat_id(
        self, supervisor_id: uuid.UUID,
    ) -> Optional[uuid.UUID]:
        async with session.begin() as conn:
            supervisor = await conn.get(Supervisor, supervisor_id)
            return supervisor.chat_id if supervisor else None

    async def get_all_active_with_chat(
        self,
    ) -> Sequence[Supervisor]:
        """
        Возвращает активных супервизоров, у которых есть chat_id.
        Используется фоновыми задачами для эскалации.
        """
        async with session.begin() as conn:
            stmt = (
                select(Supervisor)
                .where(
                    Supervisor.is_active.is_(True),
                    Supervisor.chat_id.is_not(None),
                )
            )
            result = await conn.execute(stmt)
            return result.scalars().all()


class ShiftRepo:
    """Репозиторий смен специалистов."""

    async def create_shift(
        self,
        support_id: uuid.UUID,
        planned_duration_hours: Optional[int],
    ) -> Shift:
        async with session.begin() as conn:
            shift = Shift(
                support_id=support_id,
                planned_duration_hours=planned_duration_hours,
            )
            conn.add(shift)
            await conn.flush()
            return shift

    async def get_active_shift(
        self, support_id: uuid.UUID,
    ) -> Optional[Shift]:
        async with session.begin() as conn:
            stmt = (
                select(Shift)
                .where(
                    Shift.support_id == support_id,
                    Shift.ended_at.is_(None),
                )
                .order_by(desc(Shift.started_at))
                .limit(1)
            )
            result = await conn.execute(stmt)
            return result.scalars().first()

    async def end_shift(
        self,
        shift_id: int,
        reason: str,
        ended_by: Optional[uuid.UUID] = None,
    ) -> bool:
        """
        Атомарно завершает смену. Возвращает False, если уже закрыта.
        """
        now = datetime.now(MSK)
        stmt = (
            update(Shift)
            .where(Shift.shift_id == shift_id, Shift.ended_at.is_(None))
            .values(ended_at=now, end_reason=reason, ended_by=ended_by)
            .returning(Shift.shift_id)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.first() is not None

    async def end_all_active_for_support(
        self,
        support_id: uuid.UUID,
        reason: str,
        ended_by: Optional[uuid.UUID] = None,
    ) -> int:
        """Закрывает все активные смены специалиста (на случай аномалий)."""
        now = datetime.now(MSK)
        stmt = (
            update(Shift)
            .where(
                Shift.support_id == support_id,
                Shift.ended_at.is_(None),
            )
            .values(ended_at=now, end_reason=reason, ended_by=ended_by)
        )
        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.rowcount or 0

    async def get_shifts_by_support(
        self,
        support_id: uuid.UUID,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
        limit: int = 100,
    ) -> Sequence[Shift]:
        stmt = select(Shift).where(Shift.support_id == support_id)
        if from_dt is not None:
            stmt = stmt.where(Shift.started_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(Shift.started_at <= to_dt)
        stmt = stmt.order_by(desc(Shift.started_at)).limit(limit)

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalars().all()


class WorkSessionRepo:
    """Репозиторий сессий работы над тикетами."""

    async def start_session(
        self,
        ticket_id: int,
        support_id: uuid.UUID,
        shift_id: Optional[int],
    ) -> Optional[TicketWorkSession]:
        """
        Открывает сессию работы. Если уже есть активная — не создаёт дубль.
        Атомарно: partial unique index защищает от гонок.
        """
        async with session.begin() as conn:
            # Проверяем, нет ли уже активной сессии для тикета
            stmt = (
                select(TicketWorkSession)
                .where(
                    TicketWorkSession.ticket_id == ticket_id,
                    TicketWorkSession.ended_at.is_(None),
                )
            )
            existing = (await conn.execute(stmt)).scalars().first()
            if existing is not None:
                return existing

            session_obj = TicketWorkSession(
                ticket_id=ticket_id,
                support_id=support_id,
                shift_id=shift_id,
            )
            conn.add(session_obj)
            await conn.flush()
            return session_obj

    async def end_session(
        self,
        ticket_id: int,
        reason: str,
    ) -> Optional[TicketWorkSession]:
        """
        Атомарно закрывает активную сессию тикета.
        duration_seconds вычисляется в SQL.
        """
        now = datetime.now(MSK)
        async with session.begin() as conn:
            stmt = (
                select(TicketWorkSession)
                .where(
                    TicketWorkSession.ticket_id == ticket_id,
                    TicketWorkSession.ended_at.is_(None),
                )
                .order_by(desc(TicketWorkSession.started_at))
                .limit(1)
            )
            ws = (await conn.execute(stmt)).scalars().first()
            if ws is None:
                return None

            ws.ended_at = now
            ws.end_reason = reason
            delta = now - ws.started_at
            ws.duration_seconds = max(0, int(delta.total_seconds()))
            return ws

    async def end_all_for_support(
        self,
        support_id: uuid.UUID,
        reason: str,
    ) -> int:
        """
        Закрывает все активные сессии специалиста
        (при завершении смены).
        """
        now = datetime.now(MSK)
        async with session.begin() as conn:
            stmt = (
                select(TicketWorkSession)
                .where(
                    TicketWorkSession.support_id == support_id,
                    TicketWorkSession.ended_at.is_(None),
                )
            )
            sessions = (await conn.execute(stmt)).scalars().all()
            for ws in sessions:
                ws.ended_at = now
                ws.end_reason = reason
                delta = now - ws.started_at
                ws.duration_seconds = max(0, int(delta.total_seconds()))
            return len(sessions)

    async def get_sessions_by_support(
        self,
        support_id: uuid.UUID,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
        limit: int = 1000,
    ) -> Sequence[TicketWorkSession]:
        stmt = select(TicketWorkSession).where(
            TicketWorkSession.support_id == support_id,
        )
        if from_dt is not None:
            stmt = stmt.where(TicketWorkSession.started_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(TicketWorkSession.started_at <= to_dt)
        stmt = stmt.order_by(desc(TicketWorkSession.started_at)).limit(limit)

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_total_seconds(
        self,
        support_id: uuid.UUID,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
    ) -> int:
        stmt = select(func.coalesce(func.sum(TicketWorkSession.duration_seconds), 0)).where(
            TicketWorkSession.support_id == support_id,
        )
        if from_dt is not None:
            stmt = stmt.where(TicketWorkSession.started_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(TicketWorkSession.started_at <= to_dt)

        async with session.begin() as conn:
            result = await conn.execute(stmt)
            return int(result.scalar() or 0)