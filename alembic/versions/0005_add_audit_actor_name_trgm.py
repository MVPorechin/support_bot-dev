# alembic/versions/0005_add_audit_actor_name_trgm.py

"""add trgm index on audit_logs.actor_name

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-14
"""
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_audit_actor_name_trgm "
            "ON audit_logs USING gin (actor_name gin_trgm_ops)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "DROP INDEX CONCURRENTLY IF EXISTS idx_audit_actor_name_trgm"
        )