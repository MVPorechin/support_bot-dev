"""add audit_logs table

Revision ID: 0003
Revises: 0003
Create Date: 2026-09-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_huid", UUID(as_uuid=True), nullable=True),
        sa.Column("actor_name", sa.String(length=255), nullable=True),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("action", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=20),
                  server_default="info", nullable=False),
        sa.Column("entity_type", sa.String(length=50), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("old_value", JSONB(), nullable=True),
        sa.Column("new_value", JSONB(), nullable=True),
        sa.Column("context", JSONB(), nullable=True),
        sa.Column("correlation_id", UUID(as_uuid=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "actor_type IN ('user', 'support', 'supervisor', 'system')",
            name="chk_audit_actor_type",
        ),
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'critical')",
            name="chk_audit_severity",
        ),
    )

    op.create_index("idx_audit_created_at", "audit_logs", ["created_at"])
    op.create_index("idx_audit_actor_huid", "audit_logs", ["actor_huid"])
    op.create_index("idx_audit_actor_type", "audit_logs", ["actor_type"])
    op.create_index("idx_audit_action", "audit_logs", ["action"])
    op.create_index("idx_audit_correlation_id", "audit_logs", ["correlation_id"])
    op.create_index(
        "idx_audit_entity",
        "audit_logs",
        ["entity_type", "entity_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_audit_entity", table_name="audit_logs")
    op.drop_index("idx_audit_correlation_id", table_name="audit_logs")
    op.drop_index("idx_audit_action", table_name="audit_logs")
    op.drop_index("idx_audit_actor_type", table_name="audit_logs")
    op.drop_index("idx_audit_actor_huid", table_name="audit_logs")
    op.drop_index("idx_audit_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")