"""
提示词相关 Pydantic Schema：请求参数、响应结构体。

命名约定：
- XxxBase：共享字段
- XxxOut：对外返回的响应结构（字段经过裁剪，不暴露内部字段）
- XxxListItem：列表场景下的精简字段（瀑布流不需要完整提示词原文等重字段）
"""

import uuid
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, field_validator

from app.utils.model_labels import normalize_model_labels


class SortOption(str, Enum):
    """首页/分类页排序方式，对应 PRD 3.1 的"最新/热门/浏览量"三种切换。"""

    RECOMMENDED = "recommended"
    LATEST = "latest"
    POPULAR = "popular"  # 按点赞数
    HOT = "hot"  # 按浏览量


class PromptListItem(BaseModel):
    """首页瀑布流卡片所需的精简字段，不包含提示词原文（减小响应体积）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    category: str
    models: list[str]

    _normalize_models = field_validator("models", mode="before")(normalize_model_labels)

    thumbnail_url: str | None = None  # 来自第一张效果图
    like_count: int
    view_count: int
    author_name: str | None = None
    created_at: datetime


class PromptListResponse(BaseModel):
    """首页/分类页/搜索结果的分页响应结构。"""

    items: list[PromptListItem]
    total: int
    page: int
    page_size: int
    has_more: bool


class PromptImageOut(BaseModel):
    """效果图信息，用于详情接口。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    image_url: str
    thumbnail_url: str | None = None
    sort_order: int


class PromptDetailOut(BaseModel):
    """
    详情弹窗所需的完整字段，对应 PRD 3.2。

    is_liked / is_collected 字段仅在用户已登录时才有意义，未登录时固定为 False，
    由路由层根据当前用户上下文动态填充，不在模型本身存储。
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    prompt_text: str
    description: str | None = None
    category: str
    models: list[str]

    _normalize_models = field_validator("models", mode="before")(normalize_model_labels)

    author_name: str | None = None
    author_avatar_url: str | None = None
    source_url: str | None = None
    like_count: int
    view_count: int
    collect_count: int
    created_at: datetime
    images: list[PromptImageOut]

    # 以下两个字段由路由层在返回前手动赋值，不是数据库字段
    is_liked: bool = False
    is_collected: bool = False


class ReportCreate(BaseModel):
    """举报/下架申诉请求体，对应 PRD 3.2"内容有误或需要下架？联系我们"。"""

    reporter_email: str | None = None
    reason: str
