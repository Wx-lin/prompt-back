"""Store source author avatar URLs on crawled drafts and published prompts."""

import sqlalchemy as sa

from alembic import op

revision = "5c8e9f0a12b3"
down_revision = "b61af280de43"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("crawl_drafts", sa.Column("author_avatar_url", sa.String(1000), nullable=True))
    op.add_column("prompts", sa.Column("author_avatar_url", sa.String(1000), nullable=True))


def downgrade():
    op.drop_column("prompts", "author_avatar_url")
    op.drop_column("crawl_drafts", "author_avatar_url")
