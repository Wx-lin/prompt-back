"""Attach generated private assets to conversation turns."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e81d4a9b216f"
down_revision = "d93c70f6b321"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("prompt_analyses", sa.Column("output_asset_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key(
        "fk_prompt_analysis_output_asset_owner", "prompt_analyses", "prompt_analysis_assets",
        ["output_asset_id", "user_id"], ["id", "user_id"],
    )
    op.create_index("ix_prompt_analyses_output_asset", "prompt_analyses", ["output_asset_id"])


def downgrade():
    connection = op.get_bind()
    has_images = connection.execute(
        sa.text("SELECT 1 FROM prompt_analyses WHERE output_asset_id IS NOT NULL LIMIT 1")
    ).first()
    if has_images:
        raise RuntimeError("Cannot downgrade while generated image records exist")
    op.drop_index("ix_prompt_analyses_output_asset", table_name="prompt_analyses")
    op.drop_constraint("fk_prompt_analysis_output_asset_owner", "prompt_analyses", type_="foreignkey")
    op.drop_column("prompt_analyses", "output_asset_id")
