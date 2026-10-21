"""add shifts and ticket work sessions

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-21
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Support: поле автозавершения смены ─────────────────
    op.add_column(
        "support",
        sa.Column("shift_ends_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "idx_support_shift_ends_at",
        "support", ["shift_ends_at"],
    )

    # ── Shift: смены специалистов ──────────────────────────
    op.create_table(
        "shifts",
        sa.Column("shift_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("support_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("planned_duration_hours", sa.Integer(), nullable=True),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.Column("ended_by", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.PrimaryKeyConstraint("shift_id"),
        sa.ForeignKeyConstraint(
            ["support_id"], ["support.support_id"],
            name="fk_shifts_support", ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "planned_duration_hours IS NULL OR "
            "planned_duration_hours IN (3, 4, 12)",
            name="chk_shift_duration",
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR "
            "end_reason IN ('manual', 'auto_timeout', 'supervisor')",
            name="chk_shift_end_reason",
        ),
    )
    op.create_index("idx_shifts_support_id", "shifts", ["support_id"])
    op.create_index("idx_shifts_started_at", "shifts", [sa.text("started_at DESC")])
    op.create_index(
        "idx_shifts_active",
        "shifts", ["support_id"],
        postgresql_where=sa.text("ended_at IS NULL"),
    )

    # ── TicketWorkSession: трудозатраты ────────────────────
    op.create_table(
        "ticket_work_sessions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("ticket_id", sa.Integer(), nullable=False),
        sa.Column("support_id", UUID(as_uuid=True), nullable=False),
        sa.Column("shift_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True),
            server_default=sa.text("now()"), nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("duration_seconds", sa.Integer(), nullable=True),
        sa.Column("end_reason", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["ticket_id"], ["tickets.ticket_id"],
            name="fk_work_sessions_ticket", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["support_id"], ["support.support_id"],
            name="fk_work_sessions_support", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shift_id"], ["shifts.shift_id"],
            name="fk_work_sessions_shift", ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "end_reason IS NULL OR end_reason IN "
            "('closed', 'transferred', 'pending', 'offline', "
            "'shift_timeout', 'supervisor_closed', 'auto_closed')",
            name="chk_work_session_end_reason",
        ),
    )
    op.create_index("idx_work_sessions_ticket", "ticket_work_sessions", ["ticket_id"])
    op.create_index("idx_work_sessions_support", "ticket_work_sessions", ["support_id"])
    op.create_index("idx_work_sessions_shift", "ticket_work_sessions", ["shift_id"])
    op.create_index(
        "idx_work_sessions_started_desc",
        "ticket_work_sessions", [sa.text("started_at DESC")],
    )
    # Одна активная сессия на тикет
    op.execute(
        """
        CREATE UNIQUE INDEX idx_work_sessions_active_unique
        ON ticket_work_sessions (ticket_id)
        WHERE ended_at IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_work_sessions_active_unique")
    op.drop_index("idx_work_sessions_started_desc", table_name="ticket_work_sessions")
    op.drop_index("idx_work_sessions_shift", table_name="ticket_work_sessions")
    op.drop_index("idx_work_sessions_support", table_name="ticket_work_sessions")
    op.drop_index("idx_work_sessions_ticket", table_name="ticket_work_sessions")
    op.drop_table("ticket_work_sessions")

    op.drop_index("idx_shifts_active", table_name="shifts")
    op.drop_index("idx_shifts_started_at", table_name="shifts")
    op.drop_index("idx_shifts_support_id", table_name="shifts")
    op.drop_table("shifts")

    op.drop_index("idx_support_shift_ends_at", table_name="support")
    op.drop_column("support", "shift_ends_at")