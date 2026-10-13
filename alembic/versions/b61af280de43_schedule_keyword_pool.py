"""Add keyword pools without changing existing schedule search terms."""

import sqlalchemy as sa

from alembic import op

revision = "b61af280de43"
down_revision = "9c4f8a2b6d10"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("crawl_schedules", sa.Column("keywords", sa.JSON(), nullable=False, server_default="[]"))
    op.add_column("crawl_schedules", sa.Column("last_keyword", sa.String(200), nullable=True))


def downgrade():
    op.drop_column("crawl_schedules", "last_keyword")
    op.drop_column("crawl_schedules", "keywords")
