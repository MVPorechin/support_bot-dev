import logging
from contextvars import ContextVar
from typing import Optional, Sequence
from uuid import UUID

from db.audit.repo import AuditRepo
from db.audit.models import AuditAction, AuditActorType, AuditSeverity, AuditLog

logger = logging.getLogger(__name__)

# Correlation ID текущего входящего сообщения / задачи
correlation_id_var: ContextVar[Optional[UUID]] = ContextVar(
    "correlation_id", default=None,
)


class AuditService:
    """
    Fire-and-forget аудит. Ошибки записи логируются, но не роняют основной флоу.
    """

    def __init__(self) -> None:
        self.repo = AuditRepo()

    async def log(
        self,
        *,
        action: str,
        actor_type: AuditActorType,
        entity_type: str = "ticket",
        entity_id: str | int,
        actor_huid: Optional[UUID] = None,
        actor_name: Optional[str] = None,
        severity: AuditSeverity = AuditSeverity.INFO,
        old_value: Optional[dict] = None,
        new_value: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> None:
        try:
            await self.repo.write(
                action=action,
                actor_type=actor_type,
                entity_type=entity_type,
                entity_id=entity_id,
                actor_huid=actor_huid,
                actor_name=actor_name,
                severity=severity,
                old_value=old_value,
                new_value=new_value,
                context=context,
                correlation_id=correlation_id_var.get(),
            )
        except Exception as exc:
            logger.exception(f"[AUDIT] Не удалось записать событие {action}: {exc}")

    # ── Удобные шорткаты ─────────────────────────────────────

    async def system_action(
        self,
        action: str,
        *,
        ticket_id: int,
        old: Optional[dict] = None,
        new: Optional[dict] = None,
        severity: AuditSeverity = AuditSeverity.INFO,
        context: Optional[dict] = None,
    ) -> None:
        await self.log(
            action=action,
            actor_type=AuditActorType.SYSTEM,
            actor_name="bot",
            entity_id=ticket_id,
            old_value=old,
            new_value=new,
            severity=severity,
            context=context,
        )

    async def support_action(
        self,
        action: str,
        *,
        ticket_id: int,
        support_huid: UUID,
        support_name: Optional[str] = None,
        old: Optional[dict] = None,
        new: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> None:
        await self.log(
            action=action,
            actor_type=AuditActorType.SUPPORT,
            actor_huid=support_huid,
            actor_name=support_name,
            entity_id=ticket_id,
            old_value=old,
            new_value=new,
            context=context,
        )

    async def supervisor_action(
        self,
        action: str,
        *,
        ticket_id: int,
        supervisor_huid: UUID,
        supervisor_name: Optional[str] = None,
        severity: AuditSeverity = AuditSeverity.INFO,
        context: Optional[dict] = None,
    ) -> None:
        await self.log(
            action=action,
            actor_type=AuditActorType.SUPERVISOR,
            actor_huid=supervisor_huid,
            actor_name=supervisor_name,
            entity_id=ticket_id,
            severity=severity,
            context=context,
        )

    async def user_action(
        self,
        action: str,
        *,
        ticket_id: int,
        user_huid: UUID,
        user_name: Optional[str] = None,
        old: Optional[dict] = None,
        new: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> None:
        await self.log(
            action=action,
            actor_type=AuditActorType.USER,
            actor_huid=user_huid,
            actor_name=user_name,
            entity_id=ticket_id,
            old_value=old,
            new_value=new,
            context=context,
        )

    async def get_recent_events(
        self, limit: int = 20,
    ) -> Sequence[AuditLog]:
        """Последние N событий аудита."""
        return await self.repo.get_recent(limit=limit)

# ── Lazy singleton ───────────────────────────────────────────
_audit_service: Optional[AuditService] = None


def get_audit_service() -> AuditService:
    global _audit_service
    if _audit_service is None:
        _audit_service = AuditService()
    return _audit_service