"""Group private durable jobs into bounded prompt conversations.

Revision ID: 27f86a5d9c10
Revises: f15c86be7423
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "27f86a5d9c10"
down_revision = "f15c86be7423"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("conversation_id", "parent_analysis_id"):
        op.add_column("prompt_analyses", sa.Column(name, sa.UUID(), nullable=True))
    op.add_column("prompt_analyses", sa.Column("conversation_turn", sa.Integer(), nullable=True))
    op.add_column("prompt_analyses", sa.Column("user_message", sa.Text(), nullable=True))
    op.add_column("prompt_analyses", sa.Column("context_snapshot", JSONB(none_as_null=True), nullable=True))
    op.create_unique_constraint("uq_prompt_analysis_owner", "prompt_analyses", ["id", "user_id"])
    op.create_unique_constraint(
        "uq_prompt_conversation_turn", "prompt_analyses", ["conversation_id", "conversation_turn"]
    )
    for name, column in (
        ("fk_prompt_conversation_owner", "conversation_id"),
        ("fk_prompt_parent_owner", "parent_analysis_id"),
    ):
        op.create_foreign_key(
            name, "prompt_analyses", "prompt_analyses", [column, "user_id"], ["id", "user_id"]
        )
    op.create_check_constraint(
        "ck_prompt_conversation_turn",
        "prompt_analyses",
        "conversation_turn IS NULL OR conversation_turn >= 1",
    )
    op.create_check_constraint(
        "ck_prompt_conversation_message",
        "prompt_analyses",
        "user_message IS NULL OR char_length(user_message) <= 500",
    )
    op.create_index(
        "ix_prompt_analyses_conversation",
        "prompt_analyses",
        ["user_id", "conversation_id", "conversation_turn"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM prompt_analyses WHERE conversation_id IS NOT NULL)")
    ):
        raise RuntimeError("Cannot downgrade while conversation history or accounting exists")
    op.drop_index("ix_prompt_analyses_conversation", table_name="prompt_analyses")
    for name in ("ck_prompt_conversation_message", "ck_prompt_conversation_turn"):
        op.drop_constraint(name, "prompt_analyses", type_="check")
    for name in ("fk_prompt_parent_owner", "fk_prompt_conversation_owner"):
        op.drop_constraint(name, "prompt_analyses", type_="foreignkey")
    for name in ("uq_prompt_conversation_turn", "uq_prompt_analysis_owner"):
        op.drop_constraint(name, "prompt_analyses", type_="unique")
    for name in (
        "context_snapshot",
        "user_message",
        "conversation_turn",
        "parent_analysis_id",
        "conversation_id",
    ):
        op.drop_column("prompt_analyses", name)
