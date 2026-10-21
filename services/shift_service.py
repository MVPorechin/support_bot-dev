"""
Сервис смен и учёта трудозатрат.
Связывает Support, Shift, TicketWorkSession.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from db.tickets.models import (
    Support, SupportStatus,
    Shift, ShiftEndReason,
    TicketWorkSession, WorkSessionEndReason,
)
from db.tickets.repo import SupportRepo, ShiftRepo, WorkSessionRepo

logger = logging.getLogger(__name__)
MSK = ZoneInfo("Europe/Moscow")


class ShiftService:
    def __init__(self) -> None:
        self.support_repo = SupportRepo()
        self.shift_repo = ShiftRepo()
        self.session_repo = WorkSessionRepo()

    # ── Смены ──────────────────────────────────────────────

    async def start_shift(
        self,
        support_id: uuid.UUID,
        planned_duration_hours: Optional[int],
    ) -> Optional[Shift]:
        """
        Стартует смену. planned_duration_hours=None → без ограничений.
        Также выставляет Support.status=ONLINE и shift_ends_at.
        """
        # Закрываем возможные «висящие» смены (защита от аномалий)
        await self.shift_repo.end_all_active_for_support(
            support_id, reason=ShiftEndReason.MANUAL.value,
        )

        shift = await self.shift_repo.create_shift(
            support_id=support_id,
            planned_duration_hours=planned_duration_hours,
        )

        shift_ends_at: Optional[datetime] = None
        if planned_duration_hours is not None:
            shift_ends_at = datetime.now(MSK) + timedelta(hours=planned_duration_hours)

        await self.support_repo.set_online_with_shift(
            support_id=support_id,
            shift_ends_at=shift_ends_at,
        )

        logger.info(
            f"[SHIFT] Смена #{shift.shift_id} стартовала для {support_id}, "
            f"длительность: {planned_duration_hours or '∞'}ч"
        )
        return shift

    async def end_shift(
        self,
        support_id: uuid.UUID,
        reason: str = ShiftEndReason.MANUAL.value,
        ended_by: Optional[uuid.UUID] = None,
    ) -> int:
        """
        Завершает активную смену специалиста.
        Возвращает количество закрытых work sessions.
        """
        # 1. Закрываем все work sessions (трудозатраты)
        closed_sessions = await self.session_repo.end_all_for_support(
            support_id=support_id,
            reason=WorkSessionEndReason.OFFLINE.value,
        )

        # 2. Закрываем саму смену
        active = await self.shift_repo.get_active_shift(support_id)
        if active is not None:
            await self.shift_repo.end_shift(
                shift_id=active.shift_id,
                reason=reason,
                ended_by=ended_by,
            )

        # 3. Ставим OFFLINE
        await self.support_repo.set_offline(support_id)

        logger.info(
            f"[SHIFT] Смена завершена для {support_id} "
            f"(reason={reason}, закрыто сессий: {closed_sessions})"
        )
        return closed_sessions

    async def process_expired_shifts(self) -> list[Support]:
        """
        Находит специалистов с истёкшим shift_ends_at и завершает их смены.
        Вызывается из фоновой задачи.
        """
        expired = await self.support_repo.get_expired_shifts()
        processed = []

        for specialist in expired:
            # Проверяем ещё раз — вдруг между SELECT и обработкой
            # специалист сам нажал /offline
            current = await self.support_repo.get_specialist_by_id(
                specialist.support_id,
            )
            if current is None or current.status != SupportStatus.ONLINE:
                continue

            await self.end_shift(
                support_id=specialist.support_id,
                reason=ShiftEndReason.AUTO_TIMEOUT.value,
            )
            processed.append(current)

            logger.info(
                f"[SHIFT] Автозавершение смены для {current.full_name} "
                f"по таймауту"
            )

        return processed

    # ── Трудозатраты ───────────────────────────────────────

    async def start_ticket_work(
        self,
        ticket_id: int,
        support_id: uuid.UUID,
    ) -> Optional[TicketWorkSession]:
        """
        Открывает сессию работы над тикетом.
        Берёт shift_id из активной смены (если есть).
        """
        shift = await self.shift_repo.get_active_shift(support_id)
        shift_id = shift.shift_id if shift else None

        session_obj = await self.session_repo.start_session(
            ticket_id=ticket_id,
            support_id=support_id,
            shift_id=shift_id,
        )
        logger.info(
            f"[WORK] Сессия открыта: тикет #{ticket_id}, "
            f"специалист {support_id}, shift_id={shift_id}"
        )
        return session_obj

    async def end_ticket_work(
        self,
        ticket_id: int,
        reason: str,
    ) -> Optional[TicketWorkSession]:
        """Закрывает сессию работы над тикетом."""
        session_obj = await self.session_repo.end_session(
            ticket_id=ticket_id,
            reason=reason,
        )
        if session_obj is not None:
            logger.info(
                f"[WORK] Сессия закрыта: тикет #{ticket_id}, "
                f"duration={session_obj.duration_seconds}s, reason={reason}"
            )
        return session_obj

    # ── Отчёты ─────────────────────────────────────────────

    async def get_support_labor_report(
        self,
        support_id: uuid.UUID,
        from_dt: Optional[datetime] = None,
        to_dt: Optional[datetime] = None,
    ) -> dict:
        """Сводный отчёт по трудозатратам специалиста."""
        shifts = await self.shift_repo.get_shifts_by_support(
            support_id, from_dt, to_dt,
        )
        sessions = await self.session_repo.get_sessions_by_support(
            support_id, from_dt, to_dt,
        )
        total_seconds = await self.session_repo.get_total_seconds(
            support_id, from_dt, to_dt,
        )

        return {
            "support_id": support_id,
            "shifts_count": len(shifts),
            "tickets_count": len({s.ticket_id for s in sessions}),
            "sessions_count": len(sessions),
            "total_seconds": total_seconds,
            "total_hours": round(total_seconds / 3600, 2),
            "shifts": shifts,
            "sessions": sessions,
        }