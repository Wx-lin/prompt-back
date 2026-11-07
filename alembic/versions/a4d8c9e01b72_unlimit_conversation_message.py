"""Allow long conversation messages while retaining legacy text API bounds.

Revision ID: a4d8c9e01b72
Revises: e81d4a9b216f
"""

import sqlalchemy as sa

from alembic import op

revision = "a4d8c9e01b72"
down_revision = "e81d4a9b216f"
branch_labels = None
depends_on = None


def _input_constraint(length_condition):
    return (
        "(input_type = 'image' AND asset_id IS NOT NULL AND idea_text IS NULL "
        "AND purpose IS NULL AND style IS NULL) OR "
        "(input_type = 'text' AND asset_id IS NULL AND "
        f"((idea_text IS NOT NULL AND {length_condition} "
        "AND char_length(trim(idea_text)) > 0) OR "
        "(deleted_at IS NOT NULL AND idea_text IS NULL)))"
    )


def upgrade():
    op.drop_constraint("ck_prompt_conversation_message", "prompt_analyses", type_="check")
    op.drop_constraint("ck_prompt_analysis_input", "prompt_analyses", type_="check")
    op.create_check_constraint(
        "ck_prompt_analysis_input", "prompt_analyses",
        _input_constraint(
            "char_length(idea_text) >= 1 AND "
            "(conversation_id IS NOT NULL OR char_length(idea_text) <= 500)"
        ),
    )


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM prompt_analyses "
        "WHERE char_length(user_message) > 500 OR char_length(idea_text) > 500)"
    )):
        raise RuntimeError("Cannot downgrade while long conversation messages exist")
    op.drop_constraint("ck_prompt_analysis_input", "prompt_analyses", type_="check")
    op.create_check_constraint(
        "ck_prompt_analysis_input", "prompt_analyses",
        _input_constraint("char_length(idea_text) BETWEEN 1 AND 500"),
    )
    op.create_check_constraint(
        "ck_prompt_conversation_message", "prompt_analyses",
        "user_message IS NULL OR char_length(user_message) <= 500",
    )
