"""Persist imported search-console reporting windows."""

import sqlalchemy as sa

from alembic import op

revision = "a3e6b18d4092"
down_revision = "9c35e1a62f4"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "seo_snapshots",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("engine", sa.String(10), nullable=False),
        sa.Column("dimension", sa.String(10), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.Column("rows", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("engine", "dimension", "start_date", "end_date", name="uq_seo_snapshot_window"),
    )
    op.create_index("ix_seo_snapshots_engine", "seo_snapshots", ["engine"])


def downgrade():
    op.drop_table("seo_snapshots")
