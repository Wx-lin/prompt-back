"""Separate automatic Google source and durable synchronization status."""

import sqlalchemy as sa

from alembic import op

revision = "b6f20a93d812"
down_revision = "e04b75ad6312"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "seo_api_days",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("dimension", sa.String(10), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("day", "dimension", name="uq_seo_api_day_dimension"),
    )
    op.create_index("ix_seo_api_days_day", "seo_api_days", ["day"])
    op.create_table(
        "seo_sync_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(40)),
        sa.Column("data_through", sa.Date()),
    )


def downgrade():
    op.drop_table("seo_sync_state")
    op.drop_index("ix_seo_api_days_day", table_name="seo_api_days")
    op.drop_table("seo_api_days")
