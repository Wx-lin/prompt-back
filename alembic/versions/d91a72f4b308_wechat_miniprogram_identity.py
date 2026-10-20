"""Independent app-scoped WeChat mini-program identity.

Revision ID: d91a72f4b308
Revises: c74d219e8a60
"""

import sqlalchemy as sa

from alembic import op

revision = "d91a72f4b308"
down_revision = "c74d219e8a60"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "wechat_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("app_id", sa.String(18), nullable=False),
        sa.Column("openid", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("app_id", "openid", name="uq_wechat_identity_app_openid"),
    )
    op.create_index("ix_wechat_identities_user_id", "wechat_identities", ["user_id"])


def downgrade():
    op.drop_index("ix_wechat_identities_user_id", table_name="wechat_identities")
    op.drop_table("wechat_identities")
