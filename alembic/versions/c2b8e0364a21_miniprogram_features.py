"""Add the mini-program creation visibility switch, initially disabled."""

import sqlalchemy as sa

from alembic import op

revision = "c2b8e0364a21"
down_revision = "a85c2f87d019"
branch_labels = None
depends_on = None


def upgrade():
    features = op.create_table(
        "miniprogram_features",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("creation_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.CheckConstraint("id = 1", name="ck_miniprogram_features_singleton"),
    )
    op.bulk_insert(features, [{"id": 1, "creation_enabled": False}])


def downgrade():
    op.drop_table("miniprogram_features")
