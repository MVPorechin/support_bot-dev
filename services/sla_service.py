import logging
import uuid
from datetime import datetime, timedelta, timezone

from pybotx import Bot, MentionBuilder

from config import BotConfig, SLAConfig
from db.tickets.models import TicketStatus, Ticket
from db.tickets.repo import TicketRepo
from services.audit_service import get_audit_service
from db.audit.models import AuditAction, AuditSeverity
from services.cache_service import cache_get, cache_set
from pybotx import KeyboardMarkup,KeyboardMarkup

logger = logging.getLogger(__name__)
# Антидубль уведомлений супервизорам
CRITICAL_NOTIFY_CACHE_PREFIX = "cache:sla_critical_notified"
CRITICAL_NOTIFY_TTL_SEC = 1800  # 30 минут

class SLAService:
    """Управление SLA: дедлайны, чекпоинты, эскалация."""



    def __init__(self):
        self.repo = TicketRepo()
        self.config = SLAConfig()

    def calculate_deadline(self, created_at: datetime) -> datetime:
        """Рассчитать дедлайн от момента создания."""
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at + timedelta(hours=self.config.DEADLINE_HOURS)

    def remaining_hours(self, ticket) -> float | None:
        """Сколько часов осталось до дедлайна."""
        if not ticket.sla_deadline:
            return None
        now = datetime.now(timezone.utc)
        deadline = ticket.sla_deadline
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        delta = deadline - now
        return round(delta.total_seconds() / 3600, 1)

    def sla_emoji(self, ticket) -> str:
        """Визуальный индикатор SLA для /list."""
        remaining = self.remaining_hours(ticket)
        if remaining is None:
            return "⚪"
        if remaining <= 0:
            return "🔴"
        if remaining <= self.config.CRITICAL_HOURS:
            return "🟠"
        if remaining <= self.config.WARNING_HOURS:
            return "🟡"
        return "🟢"

    async def check_and_notify(self, bot: Bot) -> None:
        """
        Главная проверка — вызывается из фоновой задачи.
        Проверяет все контрольные точки и breach.
        """
        now = datetime.now(timezone.utc)
        logger.info("[SLA] Запуск проверки контрольных точек...")

        # ──  Breach (дедлайн истёк) ────────────────────
        breached = await self.repo.get_tickets_approaching_sla(hours_left=0)
        for ticket in breached:
            await self.repo.mark_sla_breached(ticket.ticket_id)

            # [AUDIT] SLA breach — CRITICAL
            await get_audit_service().system_action(
                AuditAction.SLA_BREACHED,
                ticket_id=ticket.ticket_id,
                severity=AuditSeverity.CRITICAL,
                context={
                    "sla_deadline": ticket.sla_deadline.isoformat() if ticket.sla_deadline else None,
                    "task": "sla_monitor_loop",
                },
            )

            await self.repo.update_sla_checkpoint(ticket.ticket_id, "breached")
            await self._notify_support(bot, ticket, "🔴 SLA НАРУШЕН!")
            # Супервизорам — сразу, без антиспама
             # (breach фиксируется один раз благодаря sla_breached=True в WHERE)
            await self._notify_supervisors(
                ticket,
                "🔴 SLA НАРУШЕН!",
                severity = AuditSeverity.CRITICAL,
            )
            logger.warning(f"[SLA] Тикет #{ticket.ticket_id} — SLA нарушен")

        # ──  Critical (≤ 2ч) ─────────────────────────
        critical = await self.repo.get_tickets_approaching_sla(
            hours_left=self.config.CRITICAL_HOURS
        )
        for ticket in critical:
            if not self._checkpoint_passed(ticket, "critical"):
                await self.repo.update_sla_checkpoint(ticket.ticket_id, "critical")
                # [AUDIT] SLA warning
                await get_audit_service().system_action(
                    AuditAction.SLA_WARNING,
                    ticket_id=ticket.ticket_id,
                    severity=AuditSeverity.WARNING,
                    context={
                        "sla_deadline": ticket.sla_deadline.isoformat() if ticket.sla_deadline else None,
                        "task": "sla_monitor_loop",
                    },
                )

                await self._notify_support(
                    bot, ticket,
                    f"🟠 До нарушения SLA осталось ≤ {self.config.CRITICAL_HOURS}ч!"
                )
                # Супервизорам — с антиспамом (раз в 30 минут)

                if await self._should_notify_supervisors(ticket):
                    await self._notify_supervisors(
                        ticket,
                        f"🟠 До нарушения SLA осталось "
                         f"≤ {self.config.CRITICAL_HOURS}ч!",
                        severity = AuditSeverity.WARNING,
                    )
                logger.info(f"[SLA] Тикет #{ticket.ticket_id} — критическая зона")

        # ──  Warning (≤ 4ч) ──────────────────────────
        warning = await self.repo.get_tickets_approaching_sla(
            hours_left=self.config.WARNING_HOURS
        )
        for ticket in warning:
            if not self._checkpoint_passed(ticket, "warning"):
                await self.repo.update_sla_checkpoint(ticket.ticket_id, "warning")
                await self._notify_support(
                    bot, ticket,
                    f"🟡 До нарушения SLA осталось ≤ {self.config.WARNING_HOURS}ч"
                )
                logger.info(f"[SLA] Тикет #{ticket.ticket_id} — зона предупреждения")

    def _checkpoint_passed(self, ticket, checkpoint_name: str) -> bool:
        """Проверяет, был ли уже пройден этот чекпоинт."""
        meta = ticket.metadata_ or {}
        checkpoints = [c["checkpoint"] for c in meta.get("sla_checkpoints", [])]
        return checkpoint_name in checkpoints

    async def _notify_support(self, bot: Bot, ticket: Ticket, alert_text: str) -> None:
        """Отправить уведомление в тред поддержки тикета."""
        if not ticket.support_thread_message_id:
            return

        specialist_mention = ""
        if ticket.assigned_to:
            specialist_mention = f"\nНазначен: {MentionBuilder.contact(ticket.assigned_to)}"

        remaining = self.remaining_hours(ticket)
        remaining_text = f"{remaining}ч" if remaining is not None else "—"

        try:
            await bot.send_message(
                bot_id=uuid.UUID(BotConfig.BOT_ID),
                chat_id=ticket.support_thread_message_id,
                body=(
                    f"⚠️ **SLA Alert** — Тикет #{ticket.ticket_id}\n"
                    f"{alert_text}\n"
                    f"Осталось: {remaining_text} | {specialist_mention}\n"
                    f"Пользователь: {ticket.user_name}"
                ),
                wait_callback = False
            )
        except Exception as e:
            logger.error(f"[SLA] Не удалось отправить уведомление для тикета #{ticket.ticket_id}: {e}")

    async def _notify_supervisors(
        self,
        ticket: Ticket,
        alert_text: str,
        severity: AuditSeverity = AuditSeverity.WARNING,
    ) -> None:
        """
        Рассылает уведомление всем активным супервизорам с заполненным chat_id.
        Использует supervisor_bot, потому что chat_id — это чат
        супервизора с ботом-супервизором.
        """
        # Локальный импорт — чтобы не ловить циклические зависимости
        from bot.supervisor.bot import supervisor_bot
        from services.supervisor_service import SupervisorService
        try:
            svc = SupervisorService()
            supervisors = await svc.get_all_active_with_chat()
        except Exception as exc:
            logger.exception(
                f"[SLA] Не удалось загрузить супервизоров: {exc}"
            )
            return

        if not supervisors:
            logger.warning(
                f"[SLA] Нет активных супервизоров с chat_id для "
                f"уведомления о тикете #{ticket.ticket_id}"
            )
            return

        # Формируем текст один раз
        remaining = self.remaining_hours(ticket)
        remaining_text = f"{remaining}ч" if remaining is not None else "—"

        assigned_text = "—"
        if ticket.assigned_to:
            assigned_text = f"{MentionBuilder.contact(ticket.assigned_to)}"

        body = (
            f"🚨 **SLA Alert** — Тикет #{ticket.ticket_id}\n"
            f"{alert_text}\n"
            f"👤 Пользователь: {ticket.user_name or '—'}\n"
            f"🎧 Оператор: {assigned_text}\n"
            f"⏱ Осталось: {remaining_text}\n"
        )

        # Кнопки для быстрых действий супервизора

        bubbles = KeyboardMarkup()
        bubbles.add_button(
            command=f"/sv_join {ticket.ticket_id}",
            label="👁 Подключиться к тикету",
        )
        bubbles.add_button(
            command=f"/sv_transfer {ticket.ticket_id}",
            label="🔄 Перевести",
        )
        bubbles.add_button(
            command=f"/sv_audit {ticket.ticket_id}",
            label="🔍 Аудит",
        )

        # Рассылаем всем супервизорам
        for sup in supervisors:
            try:
                await supervisor_bot.send_message(
                    bot_id=uuid.UUID(BotConfig.SUPERVISOR_BOT_ID),
                    chat_id=sup.chat_id,
                    body=body,
                    bubbles=bubbles,
                    wait_callback=False,   # фон, не ждём callback
                )
                logger.info(
                    f"[SLA] Уведомление отправлено супервизору "
                    f"{sup.full_name} по тикету #{ticket.ticket_id}"
                )
            except Exception as exc:
                logger.error(
                    f"[SLA] Не удалось уведомить супервизора "
                    f"{sup.full_name}: {exc}"
                )

    async def _should_notify_supervisors(
        self, ticket: Ticket,
    ) -> bool:
        """
        Проверяет, нужно ли уведомлять супервизоров.
        Для critical — не чаще раза в 30 минут.
        """

        key = f"{CRITICAL_NOTIFY_CACHE_PREFIX}:{ticket.ticket_id}"

        try:
            already_notified = await cache_get(key)
            if already_notified:
                return False

            await cache_set(key, "1", ttl=CRITICAL_NOTIFY_TTL_SEC)
            return True
        except Exception as exc:
            # Redis упал — не блокируем уведомления из-за этого
            logger.warning(f"[SLA] Не удалось проверить кэш: {exc}")
            return True