"""Persist editable schedules and crawl execution metadata."""

from alembic import op
import sqlalchemy as sa

revision = "9c4f8a2b6d10"
down_revision = "1be76eff7d38"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "crawl_schedules",
        sa.Column("id", sa.UUID(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("platforms", sa.JSON(), nullable=False),
        sa.Column("keyword", sa.String(200), nullable=False),
        sa.Column("times", sa.JSON(), nullable=False),
        sa.Column("limit_count", sa.Integer(), nullable=False, server_default="20"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("auto_publish", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_crawl_schedules_next_run_at", "crawl_schedules", ["next_run_at"])
    for column in [
        sa.Column("schedule_id", sa.UUID(), sa.ForeignKey("crawl_schedules.id", ondelete="SET NULL")),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column("trigger_type", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("published_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("held_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stage", sa.String(50)),
    ]:
        op.add_column("crawl_tasks", column)
    op.create_index("ix_crawl_tasks_schedule_id", "crawl_tasks", ["schedule_id"])
    op.create_unique_constraint(
        "uq_crawl_schedule_run", "crawl_tasks", ["schedule_id", "scheduled_for", "source_platform"]
    )


def downgrade():
    op.drop_constraint("uq_crawl_schedule_run", "crawl_tasks", type_="unique")
    op.drop_index("ix_crawl_tasks_schedule_id", table_name="crawl_tasks")
    for name in ["stage", "held_count", "published_count", "trigger_type", "scheduled_for", "schedule_id"]:
        op.drop_column("crawl_tasks", name)
    op.drop_index("ix_crawl_schedules_next_run_at", table_name="crawl_schedules")
    op.drop_table("crawl_schedules")
