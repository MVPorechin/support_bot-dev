"""add chat_id to support

Revision ID: 0009
Revises: 0010
Create Date: 2026-10-04
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "support",
        sa.Column("chat_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "idx_support_chat_id",
        "support", ["chat_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_support_chat_id", table_name="support")
    op.drop_column("support", "chat_id")