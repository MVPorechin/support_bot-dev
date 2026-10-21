"""initial schema

Revision ID: 0000
Revises:
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

# revision identifiers, used by Alembic.
revision = '0000'
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ==========================================
    # 1. ТАБЛИЦА СПЕЦИАЛИСТОВ (SUPPORT)
    # ==========================================
    op.create_table(
        'support',
        sa.Column('support_id', UUID(as_uuid=True), nullable=False),
        sa.Column('full_name', sa.String(length=255), nullable=False),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='offline'),
        sa.PrimaryKeyConstraint('support_id'),
        sa.CheckConstraint(
            "status IN ('online', 'offline', 'break', 'training', 'incident')",
            name='chk_support_status',
        ),
    )
    op.create_index('idx_support_status', 'support', ['status'], unique=False)

    # ==========================================
    # 2. ТАБЛИЦА СУПЕРВИЗОРОВ (SUPERVISORS)
    # ==========================================
    op.create_table(
        'supervisors',
        sa.Column('supervisor_id', UUID(as_uuid=True), nullable=False),
        sa.Column('full_name', sa.String(length=255), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('supervisor_id'),
    )
    op.create_index('idx_supervisors_is_active', 'supervisors', ['is_active'], unique=False)

    # ==========================================
    # 3. ТАБЛИЦА ОБРАЩЕНИЙ (TICKETS)
    # ==========================================
    op.create_table(
        'tickets',
        sa.Column('ticket_id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('user_huid', UUID(as_uuid=True), nullable=False),
        sa.Column('user_name', sa.String(length=255), nullable=True),
        sa.Column('chat_id', UUID(as_uuid=True), nullable=False),
        sa.Column('message_body', sa.Text(), nullable=True),
        sa.Column('status', sa.String(length=50), nullable=False, server_default='new'),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('support_thread_message_id', UUID(as_uuid=True), nullable=True),
        sa.Column('assigned_to', UUID(as_uuid=True), nullable=True),
        sa.Column('taken_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('solution_time', sa.DateTime(timezone=True), nullable=True),
        sa.Column('rating', sa.Integer(), nullable=True),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('reopen_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        # SLA поля
        sa.Column('sla_deadline', sa.DateTime(timezone=True), nullable=True),
        sa.Column('sla_breached', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        # Гибкое хранилище
        sa.Column('metadata', JSONB(astext_type=sa.Text()), nullable=True, server_default=sa.text("'{}'::jsonb")),
        # Поля для контроля ответа пользователя (PENDING логика)
        sa.Column('last_operator_message_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('last_user_message_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('first_reminder_sent_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('pending_since', sa.DateTime(timezone=True), nullable=True),
        sa.Column('is_priority', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.PrimaryKeyConstraint('ticket_id'),
        sa.ForeignKeyConstraint(
            ['assigned_to'], ['support.support_id'],
            name='fk_tickets_specialist', ondelete='SET NULL',
        ),
        sa.CheckConstraint(
            "status IN ('new', 'open', 'in_work', 'pending', 'solved', 'closed')",
            name='chk_ticket_status',
        ),
        sa.CheckConstraint(
            'rating IS NULL OR (rating >= 1 AND rating <= 5)',
            name='chk_ticket_rating',
        ),
        sa.CheckConstraint(
            'reopen_count >= 0',
            name='chk_ticket_reopen_count',
        ),
    )

    # Индексы для tickets
    op.create_index('idx_tickets_user_huid', 'tickets', ['user_huid'], unique=False)
    op.create_index('idx_tickets_chat_id', 'tickets', ['chat_id'], unique=False)
    op.create_index('idx_tickets_status', 'tickets', ['status'], unique=False)
    op.create_index('idx_tickets_assigned_to', 'tickets', ['assigned_to'], unique=False)
    op.create_index('idx_tickets_sla_deadline', 'tickets', ['sla_deadline'], unique=False)
    op.create_index('idx_tickets_support_thread', 'tickets', ['support_thread_message_id'], unique=False)
    op.create_index('idx_tickets_pending_since', 'tickets', ['pending_since'], unique=False)
    op.create_index('idx_tickets_is_priority', 'tickets', ['is_priority'], unique=False)

    # ==========================================
    # 4. ТАБЛИЦА ИСТОРИИ ПЕРЕВОДОВ (TICKET_TRANSFERS)
    # ==========================================
    op.create_table(
        'ticket_transfers',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('ticket_id', sa.Integer(), nullable=False),
        sa.Column('from_huid', UUID(as_uuid=True), nullable=False),
        sa.Column('to_huid', UUID(as_uuid=True), nullable=False),
        sa.Column('comment', sa.Text(), nullable=True),
        sa.Column('transferred_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id'),
        sa.ForeignKeyConstraint(
            ['ticket_id'], ['tickets.ticket_id'],
            name='fk_ticket_transfers_ticket', ondelete='CASCADE',
        ),
    )
    op.create_index('idx_ticket_transfers_ticket_id', 'ticket_transfers', ['ticket_id'], unique=False)
    op.create_index('idx_ticket_transfers_time', 'ticket_transfers', [sa.text('transferred_at DESC')], unique=False)

    # ==========================================
    # 5. ТРИГГЕР ДЛЯ АВТООБНОВЛЕНИЯ updated_at
    # ==========================================
    op.execute("""
        CREATE OR REPLACE FUNCTION fn_update_timestamp()
        RETURNS TRIGGER AS $$
        BEGIN
            NEW.updated_at = NOW();
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trg_tickets_updated_at
        BEFORE UPDATE ON tickets
        FOR EACH ROW
        EXECUTE FUNCTION fn_update_timestamp();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tickets_updated_at ON tickets")
    op.execute("DROP FUNCTION IF EXISTS fn_update_timestamp()")

    op.drop_index('idx_ticket_transfers_time', table_name='ticket_transfers')
    op.drop_index('idx_ticket_transfers_ticket_id', table_name='ticket_transfers')
    op.drop_table('ticket_transfers')

    op.drop_index('idx_tickets_is_priority', table_name='tickets')
    op.drop_index('idx_tickets_pending_since', table_name='tickets')
    op.drop_index('idx_tickets_support_thread', table_name='tickets')
    op.drop_index('idx_tickets_sla_deadline', table_name='tickets')
    op.drop_index('idx_tickets_assigned_to', table_name='tickets')
    op.drop_index('idx_tickets_status', table_name='tickets')
    op.drop_index('idx_tickets_chat_id', table_name='tickets')
    op.drop_index('idx_tickets_user_huid', table_name='tickets')
    op.drop_table('tickets')

    op.drop_index('idx_supervisors_is_active', table_name='supervisors')
    op.drop_table('supervisors')

    op.drop_index('idx_support_status', table_name='support')
    op.drop_table('support')