"""Add nullable homepage curation order (compatible with older application versions)."""

import sqlalchemy as sa

from alembic import op

revision = "f8b24d0c51e3"
down_revision = "e7a13c9b40d2"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("prompts", sa.Column("homepage_rank", sa.Integer(), nullable=True))
    op.create_index("ix_prompts_homepage_rank", "prompts", ["homepage_rank"])


def downgrade():
    op.drop_index("ix_prompts_homepage_rank", table_name="prompts")
    op.drop_column("prompts", "homepage_rank")
