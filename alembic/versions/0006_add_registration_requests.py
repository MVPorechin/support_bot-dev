"""add support_registration_requests table

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-14
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_registration_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("support_id", UUID(as_uuid=True), nullable=False),
        sa.Column("full_name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20),
                  server_default="pending", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by", UUID(as_uuid=True), nullable=True),
        sa.Column("reject_reason", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="chk_reg_status",
        ),
    )

    op.create_index(
        "idx_reg_requests_support_id",
        "support_registration_requests", ["support_id"],
    )
    op.create_index(
        "idx_reg_requests_status",
        "support_registration_requests", ["status"],
    )
    op.create_index(
        "idx_reg_requests_created_desc",
        "support_registration_requests", [sa.text("created_at DESC")],
    )

    # Гарантия: один pending на одного кандидата
    op.execute("""
        CREATE UNIQUE INDEX idx_reg_requests_pending_unique
        ON support_registration_requests (support_id)
        WHERE status = 'pending'
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_reg_requests_pending_unique")
    op.drop_index("idx_reg_requests_created_desc",
                  table_name="support_registration_requests")
    op.drop_index("idx_reg_requests_status",
                  table_name="support_registration_requests")
    op.drop_index("idx_reg_requests_support_id",
                  table_name="support_registration_requests")
    op.drop_table("support_registration_requests")