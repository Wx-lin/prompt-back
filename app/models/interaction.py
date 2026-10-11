"""
用户互动相关 ORM 模型：UserLike（点赞记录）、UserCollect（收藏记录）。

两张表结构几乎一致，但语义不同、业务规则独立变化（例如未来收藏可能加分组/备注，
点赞不会），所以不合并成一张表，保持职责单一。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class UserLike(Base):
    """点赞记录。(user_id, prompt_id) 联合唯一，保证每人每条内容只能点赞一次。"""

    __tablename__ = "user_likes"
    __table_args__ = (UniqueConstraint("user_id", "prompt_id", name="uq_user_like"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserCollect(Base):
    """收藏记录。(user_id, prompt_id) 联合唯一，保证每人每条内容只能收藏一次。"""

    __tablename__ = "user_collects"
    __table_args__ = (UniqueConstraint("user_id", "prompt_id", name="uq_user_collect"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE")
    )
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE")
    )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
