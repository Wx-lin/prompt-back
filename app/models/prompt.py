"""
提示词相关 ORM 模型：Prompt（提示词主表）、PromptImage（效果图，一对多）。

对应产品需求：PRD 3.1 首页瀑布流、3.2 详情弹窗、3.5 内容管理。
"""

import uuid
from datetime import datetime

from sqlalchemy import ARRAY, Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class Prompt(Base):
    """
    提示词主表。

    一条记录 = 一条提示词 + 它的元信息（分类、适用模型、来源、互动统计）。
    效果图单独存在 PromptImage 表里（一对多），因为一条提示词可能配多张效果图。
    """

    __tablename__ = "prompts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    title: Mapped[str] = mapped_column(String(200), nullable=False, comment="标题")
    prompt_text: Mapped[str] = mapped_column(Text, nullable=False, comment="完整提示词原文")
    description: Mapped[str | None] = mapped_column(Text, comment="使用说明，如变量替换提示")

    category: Mapped[str] = mapped_column(String(50), nullable=False, index=True, comment="分类枚举值")
    # PostgreSQL 原生数组类型，存适用模型列表，如 ["即梦", "可灵"]
    models: Mapped[list[str]] = mapped_column(ARRAY(String(50)), default=list, comment="适用模型列表")

    author_name: Mapped[str | None] = mapped_column(String(100), comment="来源作者名")
    author_avatar_url: Mapped[str | None] = mapped_column(String(1000), comment="来源作者头像 URL")
    source_url: Mapped[str | None] = mapped_column(String(500), comment="来源链接，如小红书/抖音/X 原文地址")

    is_published: Mapped[bool] = mapped_column(Boolean, default=False, index=True, comment="是否已发布上线")

    view_count: Mapped[int] = mapped_column(Integer, default=0, comment="浏览量，由 Redis 批量回写")
    like_count: Mapped[int] = mapped_column(Integer, default=0, comment="点赞数")
    collect_count: Mapped[int] = mapped_column(Integer, default=0, comment="收藏数")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # 一对多关联效果图，按 sort_order 排序；级联删除：提示词删除时效果图一起删
    images: Mapped[list["PromptImage"]] = relationship(
        back_populates="prompt",
        cascade="all, delete-orphan",
        order_by="PromptImage.sort_order",
    )


class PromptImage(Base):
    """
    提示词效果图。一条提示词可以配多张图（如展示多次生成结果用于对比）。
    """

    __tablename__ = "prompt_images"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE"), nullable=False
    )

    image_url: Mapped[str] = mapped_column(String(500), nullable=False, comment="OSS 原图 URL")
    thumbnail_url: Mapped[str | None] = mapped_column(String(500), comment="缩略图 URL（OSS 图片处理生成）")
    sort_order: Mapped[int] = mapped_column(Integer, default=0, comment="多图展示时的排序，越小越靠前")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    prompt: Mapped["Prompt"] = relationship(back_populates="images")
