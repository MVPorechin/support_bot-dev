"""add supervisor_invites table

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "supervisor_invites",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", UUID(as_uuid=True), nullable=False),
        sa.Column("created_by", UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_by", UUID(as_uuid=True), nullable=True),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", UUID(as_uuid=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_supervisor_invites_code"),
    )

    op.create_index(
        "idx_supervisor_invites_code",
        "supervisor_invites", ["code"],
    )
    op.create_index(
        "idx_supervisor_invites_created_by",
        "supervisor_invites", ["created_by"],
    )
    op.create_index(
        "idx_supervisor_invites_expires_at",
        "supervisor_invites", ["expires_at"],
    )
    op.create_index(
        "idx_supervisor_invites_active",
        "supervisor_invites", ["expires_at"],
        postgresql_where=sa.text("used_at IS NULL AND revoked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("idx_supervisor_invites_active", table_name="supervisor_invites")
    op.drop_index("idx_supervisor_invites_expires_at", table_name="supervisor_invites")
    op.drop_index("idx_supervisor_invites_created_by", table_name="supervisor_invites")
    op.drop_index("idx_supervisor_invites_code", table_name="supervisor_invites")
    op.drop_table("supervisor_invites")