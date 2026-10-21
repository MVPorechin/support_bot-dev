import csv
import io
import logging
from datetime import datetime
from typing import List, Dict, Any, Optional, Sequence
from aiofiles.tempfile import NamedTemporaryFile

from db.tickets.models import Ticket
from db.tickets.repo import TicketRepo

logger = logging.getLogger(__name__)

# Единый набор колонок для всех экспортов тикетов
TICKET_CSV_FIELDS = [
    "ticket_id",
    "user_name",
    "user_huid",
    "status",
    "created_at",
    "updated_at",
    "assigned_to_name",
    "assigned_to_huid",
    "taken_at",
    "solution_time",
    "rating",
    "comment",
    "reopen_count",
    "closed_at",
    "sla_deadline",
    "sla_breached",
]


class ExportService:
    """Сервис для экспорта данных из БД в различные форматы."""

    def __init__(self):
        self.ticket_repo = TicketRepo()

    async def export_tickets_to_csv(self) -> str:
        """
        Экспортирует все тикеты в CSV формат.
        Возвращает путь к временному файлу.
        """
        try:
            # Получаем все тикеты с информацией о специалистах
            tickets_data = await self.ticket_repo.get_all_tickets_with_specialist()

            # Создаем временный файл
            async with NamedTemporaryFile("wb+", delete=False, suffix=".csv") as async_buffer:
                # Формируем CSV
                csv_buffer = io.StringIO()
                writer = csv.DictWriter(
                    csv_buffer,
                    fieldnames=[
                        'ticket_id',
                        'user_huid',
                        'status',
                        'created_at',
                        'updated_at',
                        'assigned_to_name',
                        'assigned_to_huid',
                        'taken_at',
                        'solution_time',
                        'rating',
                        'comment',
                        'reopen_count',
                        'closed_at',
                        'sla_deadline',
                        'sla_breached',
                    ],
                    delimiter=';',  # Используем ; для лучшей совместимости с Excel
                    quoting=csv.QUOTE_MINIMAL
                )

                writer.writeheader()

                if not tickets_data:
                    logger.warning("Нет тикетов для экспорта")
                    writer.writerow({
                        'ticket_id': 'Нет данных',
                        'user_huid': '',
                        'status': '',
                        'created_at': '',
                        'updated_at': '',
                        'assigned_to_name': '',
                        'assigned_to_huid': '',
                        'taken_at': '',
                        'solution_time': '',
                        'rating': '',
                        'comment': '',
                        'reopen_count': '',
                        'closed_at': '',
                        'sla_deadline': '',
                        'sla_breached': '',
                    })
                else:
                    for ticket in tickets_data:
                        # Форматируем даты для читаемости
                        row = {
                            'ticket_id': ticket['ticket_id'],
                            'user_huid': str(ticket['user_huid']),
                            'status': ticket['status'],
                            'created_at': self._format_datetime(ticket['created_at']),
                            'updated_at': self._format_datetime(ticket['updated_at']),
                            'assigned_to_name': ticket['assigned_to_name'] or 'Не назначен',
                            'assigned_to_huid': str(ticket['assigned_to_huid']) if ticket['assigned_to_huid'] else '',
                            'taken_at': self._format_datetime(ticket['taken_at']),
                            'solution_time': self._format_datetime(ticket['solution_time']),
                            'rating': ticket['rating'] or '',
                            'comment': (ticket['comment'] or '').replace('\n', ' ').replace('\r', ''),
                            'reopen_count': ticket['reopen_count'],
                            'closed_at': self._format_datetime(ticket['closed_at']),
                            'sla_deadline': self._format_datetime(ticket['sla_deadline']),
                            'sla_breached': 'Да' if ticket['sla_breached'] else 'Нет',
                        }
                        writer.writerow(row)

                # Записываем CSV в файл с BOM для корректного отображения кириллицы в Excel
                csv_content = csv_buffer.getvalue().encode('utf-8-sig')
                await async_buffer.write(csv_content)
                await async_buffer.flush()

                # Получаем путь к файлу
                file_path = async_buffer.name

            logger.info(f"Экспортировано {len(tickets_data) if tickets_data else 0} тикетов в CSV: {file_path}")
            return file_path

        except Exception as e:
            logger.exception(f"Ошибка при экспорте тикетов в CSV: {e}")
            raise

    def _format_datetime(self, dt_value: datetime) -> str:
        """Форматирует datetime в читаемый формат."""
        if dt_value is None:
            return ''
        return dt_value.strftime('%Y-%m-%d %H:%M:%S')

    async def export_messages_to_csv(self) -> str:
        """
        Экспортирует всю переписку из тикетов в CSV формат.
        Возвращает путь к временному файлу.
        """
        try:
            # Получаем все тикеты с перепиской
            tickets_data = await self.ticket_repo.get_all_tickets_with_messages()

            # Создаем временный файл
            async with NamedTemporaryFile("wb+", delete=False, suffix=".csv") as async_buffer:
                # Формируем CSV
                csv_buffer = io.StringIO()
                writer = csv.DictWriter(
                    csv_buffer,
                    fieldnames=[
                        'ticket_id',
                        'user_huid',
                        'status',
                        'created_at',
                        'assigned_to_name',
                        'message_body',
                    ],
                    delimiter=';',
                    quoting=csv.QUOTE_ALL  # Кавычки для всех полей, так как message_body может содержать спецсимволы
                )

                writer.writeheader()

                if not tickets_data:
                    logger.warning("Нет тикетов для экспорта переписки")
                    writer.writerow({
                        'ticket_id': 'Нет данных',
                        'user_huid': '',
                        'status': '',
                        'created_at': '',
                        'assigned_to_name': '',
                        'message_body': '',
                    })
                else:
                    for ticket in tickets_data:
                        # Очищаем message_body от переносов строк для корректного CSV
                        message_body = ticket['message_body'] or ''
                        # Заменяем переносы строк на пробелы, но сохраняем структуру диалога
                        message_body = message_body.replace('\r\n', ' | ').replace('\n', ' | ').replace('\r', ' | ')

                        row = {
                            'ticket_id': ticket['ticket_id'],
                            'user_huid': str(ticket['user_huid']),
                            'status': ticket['status'],
                            'created_at': self._format_datetime(ticket['created_at']),
                            'assigned_to_name': ticket['assigned_to_name'] or 'Не назначен',
                            'message_body': message_body,
                        }
                        writer.writerow(row)

                # Записываем CSV в файл с BOM для корректного отображения кириллицы в Excel
                csv_content = csv_buffer.getvalue().encode('utf-8-sig')
                await async_buffer.write(csv_content)
                await async_buffer.flush()

                file_path = async_buffer.name

            logger.info(
                f"Экспортировано {len(tickets_data) if tickets_data else 0} тикетов с перепиской в CSV: {file_path}")
            return file_path

        except Exception as e:
            logger.exception(f"Ошибка при экспорте переписки в CSV: {e}")
            raise

    async def _write_tickets_csv(
        self, tickets: Sequence[Ticket], prefix: str,
    ) -> str:
        """
        Пишет список тикетов в CSV и возвращает путь к временному файлу.
        `prefix` — часть имени файла (support / user / period).
        """
        async with NamedTemporaryFile(
            "wb+", delete=False, suffix=".csv",
        ) as async_buffer:
            csv_buffer = io.StringIO()
            writer = csv.DictWriter(
                csv_buffer,
                fieldnames=TICKET_CSV_FIELDS,
                delimiter=";",
                quoting=csv.QUOTE_MINIMAL,
            )
            writer.writeheader()

            for ticket in tickets:
                # assigned_to_name подтягивается через relationship `specialist`
                # (lazy="selectin" в модели, поэтому без N+1)
                assigned_name = (
                    ticket.specialist.full_name
                    if getattr(ticket, "specialist", None) else "Не назначен"
                )

                writer.writerow({
                    "ticket_id": ticket.ticket_id,
                    "user_huid": str(ticket.user_huid),
                    "status": ticket.status.value if ticket.status else "",
                    "created_at": self._fmt(ticket.created_at),
                    "updated_at": self._fmt(ticket.updated_at),
                    "assigned_to_name": assigned_name,
                    "assigned_to_huid": (
                        str(ticket.assigned_to) if ticket.assigned_to else ""
                    ),
                    "taken_at": self._fmt(ticket.taken_at),
                    "solution_time": self._fmt(ticket.solution_time),
                    "rating": ticket.rating if ticket.rating is not None else "",
                    "comment": (ticket.comment or "")
                        .replace("\n", " ").replace("\r", ""),
                    "reopen_count": ticket.reopen_count,
                    "closed_at": self._fmt(ticket.closed_at),
                    "sla_deadline": self._fmt(ticket.sla_deadline),
                    "sla_breached": "Да" if ticket.sla_breached else "Нет",
                })

            csv_content = csv_buffer.getvalue().encode("utf-8-sig")
            await async_buffer.write(csv_content)
            await async_buffer.flush()
            file_path = async_buffer.name

        logger.info(
            f"[EXPORT] {prefix}: {len(tickets)} записей → {file_path}"
        )
        return file_path

    @staticmethod
    def _fmt(value: Optional[datetime]) -> str:
        if value is None:
            return ""
        return value.strftime("%Y-%m-%d %H:%M:%S")

    # ── Публичные методы ─────────────────────────────────────

    async def export_tickets_by_support(
        self, name_part: str, limit: int = 20,
    ) -> tuple[str, int, list[str]]:
        """
        Возвращает (file_path, count, matched_names).
        matched_names — список уникальных ФИО специалистов, попавших в выборку.
        """
        tickets = await self.ticket_repo.get_tickets_by_support_name(
            name_part, limit=limit,
        )
        matched = sorted({
            t.specialist.full_name
            for t in tickets
            if getattr(t, "specialist", None)
        })
        path = await self._write_tickets_csv(tickets, prefix="support")
        return path, len(tickets), matched

    async def export_tickets_by_user(
        self, name_part: str, limit: int = 20,
    ) -> tuple[str, int, list[str]]:
        tickets = await self.ticket_repo.get_tickets_by_user_name(
            name_part, limit=limit,
        )
        matched = sorted({t.user_name for t in tickets if t.user_name})
        path = await self._write_tickets_csv(tickets, prefix="user")
        return path, len(tickets), matched

    async def export_tickets_by_period(
        self,
        date_from: datetime,
        date_to: datetime,
        limit: int = 20,
    ) -> tuple[str, int, tuple[datetime, datetime]]:
        tickets = await self.ticket_repo.get_tickets_by_period(
            date_from, date_to, limit=limit,
        )
        path = await self._write_tickets_csv(tickets, prefix="period")
        return path, len(tickets), (date_from, date_to)