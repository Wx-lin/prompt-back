"""
爬虫采集草稿相关的 Pydantic Schema：草稿列表/详情、人工审核时的字段修正、转为正式内容。

对应 app/models/crawl_draft.py 和 docs/aigc-prompt-爬虫方案.md 的审核流程。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CrawlDraftImageOut(BaseModel):
    """草稿配图，image_url 由路由层拼接成可访问的本地静态文件地址（见 app/routers/admin.py）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    image_url: str
    sort_order: int


class CrawlDraftOut(BaseModel):
    """草稿详情/列表项的完整响应结构。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    source_platform: str
    source_note_id: str
    source_url: str
    raw_title: str | None = None
    raw_content: str
    author_name: str | None = None
    author_avatar_url: str | None = None
    extracted_prompt_text: str | None = None
    extracted_category: str | None = None
    extracted_models: str | None = None
    status: str
    approved_prompt_id: uuid.UUID | None = None
    review_note: str | None = None
    created_at: datetime
    reviewed_at: datetime | None = None
    images: list[CrawlDraftImageOut] = []


class CrawlDraftListResponse(BaseModel):
    """草稿列表的分页响应。"""

    items: list[CrawlDraftOut]
    total: int
    page: int
    page_size: int


class CrawlDraftReviewUpdate(BaseModel):
    """
    人工审核时修正草稿字段的请求体，所有字段可选（PATCH 语义）。

    这一步不会创建正式内容，只是把审核员整理出的"干净字段"存回草稿，
    供下一步"转为正式内容"接口直接使用。
    """

    extracted_prompt_text: str | None = None
    extracted_category: str | None = None
    extracted_models: str | None = None  # 逗号分隔字符串，如 "即梦,Midjourney"


class CrawlDraftApprove(BaseModel):
    """
    审核通过、转为正式内容的请求体。

    必须先通过 CrawlDraftReviewUpdate 把 extracted_prompt_text / extracted_category
    补全（或者在这次请求里直接传入最终值，覆盖草稿上已有的值），否则会因为缺少
    必填字段被拒绝 —— 校验逻辑在 service 层，不在 schema 层做跨字段校验。
    """

    title: str  # 最终的正式标题，通常是人工从 raw_title 整理出的更规范版本
    prompt_text: str | None = None  # 不传则用草稿上的 extracted_prompt_text
    category: str | None = None  # 不传则用草稿上的 extracted_category
    models: list[str] | None = None  # 不传则用草稿上的 extracted_models（逗号分隔字符串转数组）
    is_published: bool = False  # 默认转为正式内容后仍是草稿态，需要再单独点发布


class CrawlDraftRejectBody(BaseModel):
    """忽略草稿时的请求体（可选备注）。"""

    note: str | None = None
