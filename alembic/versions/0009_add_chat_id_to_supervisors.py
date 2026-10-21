"""add chat_id to supervisors

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-27
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "supervisors",
        sa.Column("chat_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "idx_supervisors_chat_id",
        "supervisors", ["chat_id"],
    )


def downgrade() -> None:
    op.drop_index("idx_supervisors_chat_id", table_name="supervisors")
    op.drop_column("supervisors", "chat_id")