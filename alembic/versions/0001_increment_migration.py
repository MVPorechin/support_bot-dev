"""add pending logic fields and update status check

Revision ID: 0001
Revises:
Create Date: 2026-08-27
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '0001'
down_revision = '0000'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # # 1. Добавляем недостающие колонки для логики PENDING
    # op.add_column('tickets', sa.Column('last_operator_message_at', sa.DateTime(timezone=True), nullable=True))
    # op.add_column('tickets', sa.Column('last_user_message_at', sa.DateTime(timezone=True), nullable=True))
    # op.add_column('tickets', sa.Column('first_reminder_sent_at', sa.DateTime(timezone=True), nullable=True))
    # op.add_column('tickets', sa.Column('pending_since', sa.DateTime(timezone=True), nullable=True))
    # op.add_column('tickets', sa.Column('is_priority', sa.Boolean(), nullable=False, server_default=sa.text('false')))

    # 2. Создаем индексы для новых полей
    # op.create_index('idx_tickets_pending_since', 'tickets', ['pending_since'], unique=False)
    # op.create_index('idx_tickets_is_priority', 'tickets', ['is_priority'], unique=False)

    # 3. Обновляем CHECK-констрейнт для статуса (добавляем 'pending')
    # Удаляем старый констрейнт
    op.drop_constraint('chk_ticket_status', 'tickets', type_='check')

    # Создаем новый, включающий 'pending'
    op.create_check_constraint(
        'chk_ticket_status',
        'tickets',
        "status IN ('new', 'open', 'in_work', 'pending', 'solved', 'closed')"
    )


def downgrade() -> None:
    # Откат изменений
    op.drop_constraint('chk_ticket_status', 'tickets', type_='check')
    op.create_check_constraint(
        'chk_ticket_status',
        'tickets',
        "status IN ('new', 'open', 'in_work', 'solved', 'closed')"
    )

    op.drop_index('idx_tickets_is_priority', table_name='tickets')
    op.drop_index('idx_tickets_pending_since', table_name='tickets')

    op.drop_column('tickets', 'is_priority')
    op.drop_column('tickets', 'pending_since')
    op.drop_column('tickets', 'first_reminder_sent_at')
    op.drop_column('tickets', 'last_user_message_at')
    op.drop_column('tickets', 'last_operator_message_at')