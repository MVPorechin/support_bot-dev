import enum
import uuid
import datetime as dt
from typing import Optional

from sqlalchemy import (
    BigInteger, String, DateTime, Enum, Index, func, text,
)
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from db.sqlalchemy import Base


class AuditActorType(str, enum.Enum):
    USER = "user"
    SUPPORT = "support"
    SUPERVISOR = "supervisor"
    SYSTEM = "system"


class AuditSeverity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AuditAction(str, enum.Enum):
    """Справочник действий. В БД хранится как String(100)."""

    # ── Тикет: жизненный цикл ────────────────────────────────
    TICKET_CREATED = "ticket.created"
    TICKET_ASSIGNED = "ticket.assigned"
    TICKET_REASSIGNED = "ticket.reassigned"
    TICKET_TAKEN = "ticket.taken"
    TICKET_SOLVED = "ticket.solved"
    TICKET_CLOSED = "ticket.closed"
    TICKET_CLOSED_BY_SUPERVISOR = "ticket.closed_by_supervisor"
    TICKET_AUTO_CLOSED_24H = "ticket.auto_closed_24h"
    TICKET_AUTO_CLOSED_PENDING = "ticket.auto_closed_pending"
    TICKET_REOPENED = "ticket.reopened"
    TICKET_MOVED_TO_PENDING = "ticket.moved_to_pending"
    TICKET_REACTIVATED = "ticket.reactivated"
    TICKET_RESOLVED_FROM_PENDING = "ticket.resolved_from_pending"
    TICKET_TRANSFERRED = "ticket.transferred"
    TEMPLATE_SENT = "ticket.template_sent"

    # ── Таймеры ───────────────────────────────────────────────
    REMINDER_SENT = "timer.reminder_sent"
    CLOSE_REQUEST_SENT = "timer.close_request_sent"
    CLOSE_REQUEST_RESET = "timer.close_request_reset"

    # ── SLA ───────────────────────────────────────────────────
    SLA_WARNING = "sla.warning"
    SLA_BREACHED = "sla.breached"

    # ── Оператор ──────────────────────────────────────────────
    SUPPORT_ONLINE = "support.online"
    SUPPORT_OFFLINE = "support.offline"
    SUPPORT_STATUS_CHANGED = "support.status_changed"

    #Смены______________________________________________________
    SUPPORT_SHIFT_STARTED = "support.shift_started"
    SUPPORT_SHIFT_ENDED = "support.shift_ended"
    SUPPORT_SHIFT_AUTO_ENDED = "support.shift_auto_ended"

    # ── Регистрация специалистов ──────────────────────────────
    SUPPORT_REGISTRATION_REQUESTED = "support.registration_requested"
    SUPPORT_REGISTRATION_APPROVED = "support.registration_approved"
    SUPPORT_REGISTRATION_REJECTED = "support.registration_rejected"
    SUPPORT_BULK_UPLOADED = "support.bulk_uploaded"

    # ── Супервизор: просмотр аудита ─────────────────────────
    SUPERVISOR_VIEWED_AUDIT = "supervisor.viewed_audit"
    SUPERVISOR_VIEWED_AUDIT_ACTOR = "supervisor.viewed_audit_actor"
    SUPERVISOR_VIEWED_AUDIT_CORR = "supervisor.viewed_audit_corr"
    SUPERVISOR_VIEWED_PENDING_REG = "supervisor.viewed_pending_reg"

    # ── Супервизор ────────────────────────────────────────────
    SUPERVISOR_JOINED_TICKET = "supervisor.joined_ticket"
    SUPERVISOR_EXPORT_TICKETS = "supervisor.export_tickets"
    SUPERVISOR_EXPORT_MESSAGES = "supervisor.export_messages"
    SUPERVISOR_EXPORT_BY_SUPPORT = "supervisor.export_by_support"
    SUPERVISOR_EXPORT_BY_USER = "supervisor.export_by_user"
    SUPERVISOR_EXPORT_BY_PERIOD = "supervisor.export_by_period"

    # ── регистрация по инвайтам ─────────────────
    SUPERVISOR_INVITE_CREATED = "supervisor.invite_created"
    SUPERVISOR_INVITE_USED = "supervisor.invite_used"
    SUPERVISOR_INVITE_FAILED = "supervisor.invite_failed"
    SUPERVISOR_INVITE_REVOKED = "supervisor.invite_revoked"

    # ── Обратная связь ────────────────────────────────────────
    RATING_RECEIVED = "feedback.rating"
    COMMENT_RECEIVED = "feedback.comment"


class AuditLog(Base):
    """Иммутабельный журнал действий. Только INSERT."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    # ── Актор ────────────────────────────────────────────────
    actor_huid: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )
    actor_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    actor_type: Mapped[AuditActorType] = mapped_column(
        Enum(
            AuditActorType,
            native_enum=False,
            length=20,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        index=True,
    )

    # ── Действие ─────────────────────────────────────────────
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    severity: Mapped[AuditSeverity] = mapped_column(
        Enum(
            AuditSeverity,
            native_enum=False,
            length=20,
            values_callable=lambda x: [e.value for e in x],
        ),
        default=AuditSeverity.INFO,
        server_default=AuditSeverity.INFO.value,
        nullable=False,
    )

    # ── Сущность (обычно тикет) ──────────────────────────────
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # ── Снимок состояния ─────────────────────────────────────
    old_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    context: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)

    # ── Трейсинг ─────────────────────────────────────────────
    correlation_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )

    __table_args__ = (
        Index("idx_audit_entity", "entity_type", "entity_id", "created_at"),
        Index("idx_audit_created_desc", text("created_at DESC")),
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog #{self.id} {self.action} "
            f"by {self.actor_type}:{self.actor_name} at {self.created_at}>"
        )