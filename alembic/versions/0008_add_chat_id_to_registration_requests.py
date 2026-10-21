"""add chat_id to support_registration_requests

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-25
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Добавляем nullable, потому что в таблице могут быть старые записи
    op.add_column(
        "support_registration_requests",
        sa.Column("chat_id", UUID(as_uuid=True), nullable=True),
    )

    # Индекс для быстрого поиска заявок по чату (если понадобится)
    op.create_index(
        "idx_reg_requests_chat_id",
        "support_registration_requests",
        ["chat_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "idx_reg_requests_chat_id",
        table_name="support_registration_requests",
    )
    op.drop_column("support_registration_requests", "chat_id")