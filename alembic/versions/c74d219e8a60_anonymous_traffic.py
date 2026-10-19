"""Anonymous daily browser UV and durable enablement timestamp.

Revision ID: c74d219e8a60
Revises: a3e6b18d4092
"""

import sqlalchemy as sa

from alembic import op

revision = "c74d219e8a60"
down_revision = "a3e6b18d4092"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "visitor_daily",
        sa.Column("date", sa.Date(), primary_key=True),
        sa.Column("visitor_hash", sa.String(64), primary_key=True),
    )
    op.create_table(
        "traffic_tracking_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_traffic_tracking_singleton"),
    )
    op.execute("INSERT INTO traffic_tracking_state (id, enabled_at) VALUES (1, CURRENT_TIMESTAMP)")


def downgrade():
    op.drop_table("visitor_daily")
    op.drop_table("traffic_tracking_state")
