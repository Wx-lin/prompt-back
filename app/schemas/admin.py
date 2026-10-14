"""
后台管理相关的 Pydantic Schema：内容的增删改查、举报处理。

命名约定沿用 app/schemas/prompt.py 的风格：XxxCreate 是创建请求体，
XxxUpdate 是编辑请求体（字段全部可选，支持部分更新），XxxOut 是响应结构。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.utils.category_catalog import CategoryKey


class AdminPromptImageIn(BaseModel):
    """创建/编辑内容时，效果图的输入结构（图片需要先上传到 OSS 拿到 URL，再传给这个接口）。"""

    image_url: str
    thumbnail_url: str | None = None
    sort_order: int = 0


class AdminPromptCreate(BaseModel):
    """
    后台创建一条新内容的请求体，对应采集流程里"人工审核确认后入库"这一步
    （见 docs/aigc-prompt-爬虫方案.md 3.3 节）。

    is_published 默认 False：新建的内容先是草稿态，审核员确认无误后再调用
    发布接口上线，避免有问题的内容直接出现在用户可见的首页上。
    """

    title: str
    prompt_text: str
    description: str | None = None
    category: CategoryKey
    models: list[str] = []
    author_name: str | None = None
    author_avatar_url: str | None = None
    source_url: str | None = None
    is_published: bool = False
    images: list[AdminPromptImageIn] = []


class AdminPromptUpdate(BaseModel):
    """
    后台编辑一条内容的请求体，所有字段均可选，只传需要修改的字段（PATCH 语义）。

    images 字段如果传了，会整体替换掉原有的效果图列表（不支持单张图片增删，
    MVP 阶段够用，图片数量通常不多，整体替换操作更简单）。
    """

    title: str | None = None
    prompt_text: str | None = None
    description: str | None = None
    category: CategoryKey | None = None
    models: list[str] | None = None
    author_name: str | None = None
    author_avatar_url: str | None = None
    source_url: str | None = None
    is_published: bool | None = None
    images: list[AdminPromptImageIn] | None = None

    @field_validator("category", mode="before")
    @classmethod
    def category_cannot_be_cleared(cls, value):
        if value is None:
            raise ValueError("请选择有效分类")
        return value


class AdminPromptOut(BaseModel):
    """后台内容列表/详情的完整响应结构，比对外的 PromptDetailOut 多暴露 is_published 等管理字段。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    prompt_text: str
    description: str | None = None
    category: str
    models: list[str]
    author_name: str | None = None
    author_avatar_url: str | None = None
    source_url: str | None = None
    is_published: bool
    view_count: int
    like_count: int
    collect_count: int
    created_at: datetime
    updated_at: datetime


class AdminPromptListResponse(BaseModel):
    """后台内容列表的分页响应，复用 AdminPromptOut 作为列表项。"""

    items: list[AdminPromptOut]
    total: int
    page: int
    page_size: int


class AdminReportOut(BaseModel):
    """后台举报列表的响应结构，附带被举报内容的标题，方便运营方不用再跳转查询。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    prompt_id: uuid.UUID
    prompt_title: str | None = None  # 由路由层查询 Prompt 后手动填入，不是 ContentReport 自身字段
    reporter_email: str | None = None
    reason: str
    status: str
    handled_note: str | None = None
    created_at: datetime
    handled_at: datetime | None = None


class AdminReportListResponse(BaseModel):
    """后台举报列表的分页响应。"""

    items: list[AdminReportOut]
    total: int
    page: int
    page_size: int


class AdminReportHandle(BaseModel):
    """
    处理举报的请求体，对应 PRD 3.5"下架申诉处理"（P0，24-48h 内处理）。

    unpublish_prompt=True 时，会联动把对应的 Prompt.is_published 置为 False，
    一步完成"审核通过举报理由 → 下架内容"，不需要再额外调一次编辑接口。
    """

    handled_note: str | None = None
    unpublish_prompt: bool = False
