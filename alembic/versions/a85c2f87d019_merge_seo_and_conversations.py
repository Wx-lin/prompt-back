"""Merge already released SEO history with prompt conversation migrations.

Revision ID: a85c2f87d019
Revises: b6f20a93d812, 27f86a5d9c10
"""

import sqlalchemy as sa

from alembic import op

revision = "a85c2f87d019"
down_revision = ("b6f20a93d812", "27f86a5d9c10")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    if op.get_bind().scalar(sa.text(
        "SELECT EXISTS (SELECT 1 FROM prompt_analyses "
        "WHERE input_type = 'text' OR conversation_id IS NOT NULL)"
    )):
        raise RuntimeError("Cannot downgrade while text prompt tasks exist or conversation history exists")
