"""
爬虫采集草稿 ORM 模型：CrawlDraft（采集草稿）、CrawlDraftImage（草稿配图）。

对应产品需求：docs/aigc-prompt-爬虫方案.md 3.3 节"提示词原文的提取建议人工辅助"。

设计意图：爬虫脚本只负责把原始素材（标题、正文、图片、作者信息、链接）存进这张
"草稿表"，不直接写正式的 Prompt 表。运营方在后台管理面板里人工审核草稿、补全/
修正字段后，点击"转为正式内容"，才会在 Prompt 表里创建一条正式记录。
这样可以避免自动化抓取的脏数据直接污染用户可见的正式内容。
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.database import Base


class CrawlDraft(Base):
    """
    采集草稿主表。

    status 流转：pending（待审核）→ approved（已转为正式内容）/ rejected（已忽略）。
    一条草稿审核通过后会在 Prompt 表创建一条新记录，approved_prompt_id 记录对应关系，
    避免同一条草稿被重复转正式内容。
    """

    __tablename__ = "crawl_drafts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    source_platform: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="来源平台，如 xiaohongshu"
    )
    source_note_id: Mapped[str] = mapped_column(String(100), nullable=False, comment="来源平台的笔记/帖子ID")
    source_url: Mapped[str] = mapped_column(String(500), nullable=False, comment="原始链接")

    # 以下字段是脚本抓取的"原始素材"，不是最终入库字段，审核时人工在这些素材基础上
    # 整理出 Prompt 需要的标准字段（标题/提示词原文/分类/模型标签等）。
    raw_title: Mapped[str | None] = mapped_column(String(300), comment="原始标题")
    raw_content: Mapped[str] = mapped_column(Text, nullable=False, comment="原始正文全文，提示词通常混在里面")
    author_name: Mapped[str | None] = mapped_column(String(100), comment="原始作者昵称")
    author_avatar_url: Mapped[str | None] = mapped_column(String(1000), comment="来源作者头像 URL")

    # 审核时人工填写/修正的字段，初始为空，审核通过后用这些字段生成正式的 Prompt。
    extracted_prompt_text: Mapped[str | None] = mapped_column(Text, comment="人工从正文中提取出的提示词原文")
    extracted_category: Mapped[str | None] = mapped_column(String(50), comment="人工标注的分类")
    extracted_models: Mapped[str | None] = mapped_column(
        String(200), comment="人工标注的适用模型，逗号分隔字符串（草稿阶段不用数组类型，减少迁移改动）"
    )

    status: Mapped[str] = mapped_column(
        String(20), default="pending", index=True, comment="pending / approved / rejected"
    )
    approved_prompt_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), comment="审核通过后对应生成的正式 Prompt.id，防止重复转化"
    )
    review_note: Mapped[str | None] = mapped_column(Text, comment="审核备注，常用于记录忽略原因")

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), comment="审核处理时间")

    images: Mapped[list["CrawlDraftImage"]] = relationship(
        back_populates="draft",
        cascade="all, delete-orphan",
        order_by="CrawlDraftImage.sort_order",
    )


class CrawlDraftImage(Base):
    """
    采集草稿的原始配图。

    local_path 是爬虫脚本下载到本机的文件路径（因为 OSS 密钥还没配置，当前阶段
    图片先存本地，由后端通过一个静态文件路由提供访问，详见 app/routers/admin.py
    的草稿图片访问接口）。等 OSS 配置好后，审核转正式内容时再把本地图片上传到
    OSS，正式的 PromptImage.image_url 存 OSS 地址，不会直接复用这里的本地路径。
    """

    __tablename__ = "crawl_draft_images"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("crawl_drafts.id", ondelete="CASCADE"), nullable=False
    )

    local_path: Mapped[str] = mapped_column(String(500), nullable=False, comment="本机存储路径（相对路径）")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    draft: Mapped["CrawlDraft"] = relationship(back_populates="images")
