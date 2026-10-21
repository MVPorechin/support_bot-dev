"""add pg_trgm extension and gin indexes

Revision ID: 0004
Revises: 0003
Create Date: 2026-09-14
"""
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Расширение pg_trgm (требует autocommit) ──────────
    with op.get_context().autocommit_block():
        op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # ── 2. GIN-индексы через CONCURRENTLY ───────────────────
    # Каждый — в своём autocommit-блоке.
    # CONCURRENTLY не блокирует таблицу на запись — важно для prod.
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_support_full_name_trgm "
            "ON support USING gin (full_name gin_trgm_ops)"
        )

    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_tickets_user_name_trgm "
            "ON tickets USING gin (user_name gin_trgm_ops)"
        )


def downgrade() -> None:
    # DROP INDEX CONCURRENTLY тоже требует autocommit
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_tickets_user_name_trgm")

    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS idx_support_full_name_trgm")

    # Расширение НЕ дропаем — могут использоваться другие индексы
    # Если нужно — раскомментируйте:
    # with op.get_context().autocommit_block():
    #     op.execute("DROP EXTENSION IF EXISTS pg_trgm")