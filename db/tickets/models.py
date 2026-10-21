import uuid
import datetime as dt
import enum
from typing import Optional, List

from sqlalchemy import String, DateTime, Integer, ForeignKey, Boolean, Text, Enum, func, Index, BigInteger
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.sqlalchemy import Base

class RegistrationRequestStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"

class ShiftEndReason(str, enum.Enum):
    MANUAL = "manual"              # /offline вручную
    AUTO_TIMEOUT = "auto_timeout"  # истёк shift_ends_at
    SUPERVISOR = "supervisor"      # супервизор принудительно

class WorkSessionEndReason(str, enum.Enum):
    CLOSED = "closed"
    TRANSFERRED = "transferred"
    PENDING = "pending"
    OFFLINE = "offline"
    SHIFT_TIMEOUT = "shift_timeout"
    SUPERVISOR_CLOSED = "supervisor_closed"
    AUTO_CLOSED = "auto_closed"


class TicketWorkSession(Base):
    """Сессия работы специалиста над тикетом. Учёт трудозатрат."""
    __tablename__ = "ticket_work_sessions"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("tickets.ticket_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    support_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support.support_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    shift_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("shifts.shift_id", ondelete="SET NULL"),
        nullable=True, index=True,
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    ended_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    end_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    shift: Mapped[Optional["Shift"]] = relationship(
        "Shift", back_populates="work_sessions", lazy="selectin",
    )

class SupportRegistrationRequest(Base):
    """Заявка на регистрацию специалиста. Модерируется супервизором."""

    __tablename__ = "support_registration_requests"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    # ── Кто подал заявку ─────────────────────────────────────
    support_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True,
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # ── куда отправлять ответы (личный чат бота с пользователем) ─
    chat_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )

    # ── Статус заявки ────────────────────────────────────────
    status: Mapped[RegistrationRequestStatus] = mapped_column(
        Enum(
            RegistrationRequestStatus,
            native_enum=False,
            length=20,
            values_callable=lambda x: [e.value for e in x],
        ),
        default=RegistrationRequestStatus.PENDING,
        nullable=False,
        index=True,
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    resolved_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    resolved_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    reject_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

class SupportStatus(str, enum.Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    BREAK = "break"
    TRAINING = "training"
    INCIDENT = "incident"

class TicketPriority(str, enum.Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"

class TicketStatus(str, enum.Enum):
    NEW = "new"
    OPEN = "open"
    IN_WORK = "in_work"
    PENDING = "pending"
    SOLVED = "solved"
    CLOSED = "closed"

class Ticket(Base):
    __tablename__ = "tickets"

    ticket_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_huid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    user_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    chat_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    message_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    status: Mapped[TicketStatus] = mapped_column(
        Enum(
            TicketStatus,
            native_enum=False,
            length=50,
            values_callable=lambda x: [e.value for e in x],
        ),
        default=TicketStatus.NEW,
        nullable=False,
        index=True,
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    support_thread_message_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )

    # Внешний ключ на таблицу support
    assigned_to: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support.support_id", ondelete="SET NULL"),
        nullable=True,
        index=True
    )

    taken_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    solution_time: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    closed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── SLA поля (Новое) ──────────────────────────────────
    sla_deadline: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    sla_breached: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )

    # ── Новые поля для контроля ответа пользователя ─────────────
    last_operator_message_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_user_message_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    first_reminder_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pending_since: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # ── Таймер запроса закрытия (10 минут) ───────────────
    close_request_sent_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Признак приоритетного тикета после возврата из PENDING
    is_priority: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        server_default="false",
        nullable=False,
        index=True,
    )

    # Гибкое хранилище для чекпоинтов SLA и других метаданных
    metadata_: Mapped[Optional[dict]] = mapped_column(
        "metadata", JSONB, nullable=True, default=dict
    )

    # ── Отношения ─────────────────────────────────────────
    specialist: Mapped[Optional["Support"]] = relationship(
        "Support",
        back_populates="tickets",
        lazy="selectin"
    )

    # История переводов тикета
    transfers: Mapped[List["TicketTransfer"]] = relationship(
        "TicketTransfer",
        back_populates="ticket",
        cascade="all, delete-orphan",
        lazy="selectin"
    )

class Support(Base):
    __tablename__ = "support"

    support_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[SupportStatus] = mapped_column(
        Enum(
            SupportStatus,
            native_enum=False,
            length=50,
            values_callable=lambda x: [e.value for e in x],
        ),
        default=SupportStatus.OFFLINE,
        nullable=False,
        index=True,
    )
    shift_ends_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )

    tickets: Mapped[List["Ticket"]] = relationship(
        "Ticket",
        back_populates="specialist",
        lazy="selectin"
    )
    chat_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True,
    )

class TicketTransfer(Base):
    """Аудит переводов тикета между специалистами."""
    __tablename__ = "ticket_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Связь с тикетом (обрати внимание: FK на ticket_id, а не id)
    ticket_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("tickets.ticket_id", ondelete="CASCADE"), index=True
    )

    from_huid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    to_huid: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    comment: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    transferred_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    ticket: Mapped["Ticket"] = relationship("Ticket", back_populates="transfers")

class Supervisor(Base):
    """Сущность супервизора (руководителя поддержки)."""
    __tablename__ = "supervisors"

    supervisor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # ID личного чата супервизора с ботом.
    # Заполняется автоматически при первом обращении.
    chat_id: Mapped[Optional[uuid.UUID]] = mapped_column(
            UUID(as_uuid=True), nullable=True, index=True,
    )

class SupervisorInvite(Base):
    """Приглашение на регистрацию супервизора."""

    __tablename__ = "supervisor_invites"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    code: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, index=True,
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    used_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    used_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revoked_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    revoked_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )




class Shift(Base):
    """Смена специалиста поддержки."""
    __tablename__ = "shifts"

    shift_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    support_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("support.support_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )
    ended_at: Mapped[Optional[dt.datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    planned_duration_hours: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
    )
    end_reason: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)
    ended_by: Mapped[Optional[uuid.UUID]] = mapped_column(
        UUID(as_uuid=True), nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
    )

    support: Mapped["Support"] = relationship("Support", lazy="selectin")
    work_sessions: Mapped[List["TicketWorkSession"]] = relationship(
        "TicketWorkSession", back_populates="shift", lazy="selectin",
    )
