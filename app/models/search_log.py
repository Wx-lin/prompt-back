"""
搜索日志 ORM 模型：SearchLog。

对应产品需求：PRD 搜索弹窗的"热门搜索"区域（见 aigc-prompt-后端技术选型.md 3.3 节）。
只做日志记录，不做实时聚合查询；热门排行由定时任务离线聚合后写入 Redis。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class SearchLog(Base):
    """
    搜索日志。user_id 允许为空，匿名搜索也要记录（用于统计全站热门搜索词，
    不区分是否登录）。
    """

    __tablename__ = "search_logs"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    keyword: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="搜索关键词原文，不做归一化处理"
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
