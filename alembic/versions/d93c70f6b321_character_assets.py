"""Add private character assets.

Revision ID: d93c70f6b321
Revises: c2b8e0364a21
"""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d93c70f6b321"
down_revision = "c2b8e0364a21"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "character_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("values", postgresql.JSONB(), nullable=False),
        sa.Column("template", sa.String(100), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("preview_image", sa.LargeBinary(), nullable=True),
        sa.Column("preview_signature", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_character_assets_user_id", "character_assets", ["user_id"])


def downgrade():
    op.drop_index("ix_character_assets_user_id", table_name="character_assets")
    op.drop_table("character_assets")
