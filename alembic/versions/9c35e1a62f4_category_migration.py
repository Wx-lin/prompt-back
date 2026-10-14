"""Add legacy membership and audited category migration; no data changes."""

import sqlalchemy as sa

from alembic import op

revision = "9c35e1a62f4"
down_revision = "f8b24d0c51e3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "category_legacy_memberships",
        sa.Column("legacy_key", sa.String(50), primary_key=True),
        sa.Column("prompt_id", sa.UUID(), sa.ForeignKey("prompts.id", ondelete="CASCADE"), primary_key=True),
    )
    op.create_table(
        "category_migration_changes",
        sa.Column("batch_id", sa.String(64), primary_key=True),
        sa.Column("entity", sa.String(20), primary_key=True),
        sa.Column("record_id", sa.UUID(), primary_key=True),
        sa.Column("before_state", sa.JSON(), nullable=False),
        sa.Column("after_state", sa.JSON(), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade():
    op.drop_table("category_migration_changes")
    op.drop_table("category_legacy_memberships")
