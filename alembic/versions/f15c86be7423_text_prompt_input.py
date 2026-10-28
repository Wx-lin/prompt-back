"""Add private text-to-prompt inputs without changing existing image jobs.

Revision ID: f15c86be7423
Revises: e04b75ad6312
"""

import sqlalchemy as sa

from alembic import op

revision = "f15c86be7423"
down_revision = "e04b75ad6312"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("prompt_analyses", sa.Column(
        "input_type", sa.String(10), nullable=False, server_default="image",
    ))
    op.add_column("prompt_analyses", sa.Column("idea_text", sa.Text(), nullable=True))
    op.add_column("prompt_analyses", sa.Column("purpose", sa.String(30), nullable=True))
    op.add_column("prompt_analyses", sa.Column("style", sa.String(30), nullable=True))
    op.alter_column("prompt_analyses", "asset_id", existing_type=sa.UUID(), nullable=True)
    op.create_check_constraint(
        "ck_prompt_analysis_input", "prompt_analyses",
        "(input_type = 'image' AND asset_id IS NOT NULL AND idea_text IS NULL "
        "AND purpose IS NULL AND style IS NULL) OR "
        "(input_type = 'text' AND asset_id IS NULL AND "
        "((idea_text IS NOT NULL AND char_length(idea_text) BETWEEN 1 AND 500 "
        "AND char_length(trim(idea_text)) > 0) OR "
        "(deleted_at IS NOT NULL AND idea_text IS NULL)))",
    )
    op.create_check_constraint(
        "ck_prompt_analysis_purpose", "prompt_analyses",
        "purpose IS NULL OR purpose IN ('wallpaper','avatar','poster','product','illustration')",
    )
    op.create_check_constraint(
        "ck_prompt_analysis_style", "prompt_analyses",
        "style IS NULL OR style IN ('cinematic','photorealistic','illustration','anime','minimalist')",
    )


def downgrade():
    # Refuse data loss: every text task, including tombstones, is private history
    # or accounting/idempotency state that cannot be converted to an image job.
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM prompt_analyses WHERE input_type = 'text')"
    )):
        raise RuntimeError("Cannot downgrade while text prompt tasks exist")
    op.drop_constraint("ck_prompt_analysis_style", "prompt_analyses", type_="check")
    op.drop_constraint("ck_prompt_analysis_purpose", "prompt_analyses", type_="check")
    op.drop_constraint("ck_prompt_analysis_input", "prompt_analyses", type_="check")
    op.alter_column("prompt_analyses", "asset_id", existing_type=sa.UUID(), nullable=False)
    for name in ("style", "purpose", "idea_text", "input_type"):
        op.drop_column("prompt_analyses", name)
