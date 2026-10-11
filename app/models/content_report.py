"""
内容举报/下架申诉 ORM 模型：ContentReport。

对应产品需求：PRD 3.2 详情弹窗"举报/申诉入口"（P0），以及合规要求中必须提供的
内容下架申诉渠道。本功能在最初的《后端技术选型.md》中曾遗漏，这里补齐。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class ContentReport(Base):
    """
    内容举报记录。任何人（含匿名）可对某条提示词发起下架申诉，运营方人工处理。

    status 流转：pending（待处理）→ resolved（已处理，内容已下架或驳回）。
    """

    __tablename__ = "content_reports"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE")
    )

    reporter_email: Mapped[str | None] = mapped_column(String(200), comment="申诉人联系邮箱，便于后续沟通")
    reason: Mapped[str] = mapped_column(Text, nullable=False, comment="申诉原因说明")

    status: Mapped[str] = mapped_column(String(20), default="pending", comment="处理状态：pending / resolved")
    handled_note: Mapped[str | None] = mapped_column(Text, comment="运营方处理备注")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    handled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="处理完成时间")
