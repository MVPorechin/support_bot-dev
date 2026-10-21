import logging
from typing import Optional, Sequence
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, desc, delete

from db.sqlalchemy import session
from db.audit.models import AuditLog

logger = logging.getLogger(__name__)


class AuditRepo:
    """Только INSERT + SELECT + ретеншен. Никаких UPDATE."""

    async def get_recent(self, limit: int = 20) -> Sequence[AuditLog]:
        """Последние N событий (для /sv_audit_recent)."""
        async with session.begin() as conn:
            stmt = (
                select(AuditLog)
                .order_by(desc(AuditLog.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def write(
        self,
        *,
        action: str,
        actor_type: str,
        entity_type: str,
        entity_id: str | int,
        actor_huid=None,
        actor_name: Optional[str] = None,
        severity: str = "info",
        old_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        context: Optional[dict] = None,
        correlation_id=None,
    ) -> None:
        record = AuditLog(
            actor_huid=actor_huid,
            actor_name=actor_name,
            actor_type=actor_type,
            action=action,
            severity=severity,
            entity_type=entity_type,
            entity_id=str(entity_id),
            old_value=old_value,
            new_value=new_value,
            context=context,
            correlation_id=correlation_id,
        )
        async with session.begin() as conn:
            conn.add(record)

    async def get_by_entity(
        self,
        entity_type: str,
        entity_id: str | int,
        limit: int = 200,
    ) -> Sequence[AuditLog]:
        async with session.begin() as conn:
            stmt = (
                select(AuditLog)
                .where(
                    AuditLog.entity_type == entity_type,
                    AuditLog.entity_id == str(entity_id),
                )
                .order_by(desc(AuditLog.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_by_actor(
        self, actor_huid, limit: int = 200,
    ) -> Sequence[AuditLog]:
        async with session.begin() as conn:
            stmt = (
                select(AuditLog)
                .where(AuditLog.actor_huid == actor_huid)
                .order_by(desc(AuditLog.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_by_correlation(
        self, correlation_id, limit: int = 200,
    ) -> Sequence[AuditLog]:
        async with session.begin() as conn:
            stmt = (
                select(AuditLog)
                .where(AuditLog.correlation_id == correlation_id)
                .order_by(AuditLog.created_at)
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_recent(self, limit: int = 20) -> Sequence[AuditLog]:
        """
        Последние N событий аудита (по убыванию created_at).
        Используется в /sv_audit_recent.
        """
        async with session.begin() as conn:
            stmt = (
                select(AuditLog)
                .order_by(desc(AuditLog.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def purge_older_than(self, days: int) -> int:
        """Ретеншен. Вызывается из фоновой задачи раз в сутки."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        async with session.begin() as conn:
            stmt = delete(AuditLog).where(AuditLog.created_at < cutoff)
            result = await conn.execute(stmt)
            return result.rowcount or 0

    async def get_by_actor_name(
        self, name_part: str, limit: int = 100,
    ) -> Sequence[AuditLog]:
        """
        Ищет события по подстроке ФИО актора (case-insensitive).
        Например, «иванов» найдёт «Иванов Иван Иванович».
        """
        pattern = f"%{name_part.strip()}%"
        async with session.begin() as conn:
            stmt = (
                select(AuditLog)
                .where(AuditLog.actor_name.ilike(pattern))
                .order_by(desc(AuditLog.created_at))
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return result.scalars().all()

    async def get_actor_huids_by_name(
        self, name_part: str, limit: int = 50,
    ) -> list[tuple]:
        """
        Возвращает уникальные пары (actor_huid, actor_name) для построения кнопок.
        Используется, когда по подстроке находится несколько сотрудников.
        """
        pattern = f"%{name_part.strip()}%"
        async with session.begin() as conn:
            stmt = (
                select(AuditLog.actor_huid, AuditLog.actor_name)
                .where(
                    AuditLog.actor_name.ilike(pattern),
                    AuditLog.actor_huid.is_not(None),
                )
                .group_by(AuditLog.actor_huid, AuditLog.actor_name)
                .limit(limit)
            )
            result = await conn.execute(stmt)
            return [(row[0], row[1]) for row in result.all()]