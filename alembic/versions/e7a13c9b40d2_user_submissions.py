"""Add user submissions and owned upload assets."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e7a13c9b40d2"
down_revision = "5c8e9f0a12b3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("prompts", sa.Column("submitter_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("prompts", sa.Column("review_status", sa.String(20), nullable=True))
    op.add_column("prompts", sa.Column("review_note", sa.Text(), nullable=True))
    op.add_column("prompts", sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_prompts_submitter", "prompts", "users", ["submitter_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_prompts_submitter_id", "prompts", ["submitter_id"])
    op.create_index("ix_prompts_review_status", "prompts", ["review_status"])
    op.create_table(
        "submission_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_key", sa.String(200), unique=True, nullable=False),
        sa.Column("image_url", sa.String(500), nullable=False),
        sa.Column(
            "prompt_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("prompts.id", ondelete="SET NULL")
        ),
        sa.Column("used_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_submission_assets_user_id", "submission_assets", ["user_id"])
    op.create_index("ix_submission_assets_prompt_id", "submission_assets", ["prompt_id"])


def downgrade():
    op.drop_table("submission_assets")
    op.drop_index("ix_prompts_review_status", table_name="prompts")
    op.drop_index("ix_prompts_submitter_id", table_name="prompts")
    op.drop_constraint("fk_prompts_submitter", "prompts", type_="foreignkey")
    for column in ("reviewed_at", "review_note", "review_status", "submitter_id"):
        op.drop_column("prompts", column)
