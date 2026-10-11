"""
采集任务相关的 Pydantic Schema：开始采集的请求体、任务状态响应。

对应 app/models/crawl_task.py 和 app/services/crawl_task_service.py。
"""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CrawlTaskStartRequest(BaseModel):
    """
    开始一批采集任务的请求体，支持多选平台。

    勾选多个平台会创建多条任务（一个平台一条），共享同一个 batch_id，按顺序
    排队执行，详见 app/services/crawl_task_service.py 的 start_crawl_batch。
    """

    platforms: list[str] = Field(min_length=1, description="平台列表：xiaohongshu / weibo / kuaishou")
    keyword: str = Field(min_length=1, max_length=200, description="搜索关键词")
    limit: int = Field(default=10, ge=1, le=50, description="每个平台期望采集的笔记数量上限")

    @field_validator("platforms")
    @classmethod
    def _dedup_platforms(cls, value: list[str]) -> list[str]:
        """去重但保持用户勾选的顺序，避免重复平台导致跑两次。"""
        seen: list[str] = []
        for p in value:
            if p not in seen:
                seen.append(p)
        return seen


class CrawlTaskOut(BaseModel):
    """任务状态响应，前端用这个结构轮询任务进度。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    batch_id: uuid.UUID
    source_platform: str
    keyword: str
    limit_count: int
    status: str  # pending / running / success / failed
    new_draft_count: int
    schedule_id: uuid.UUID | None = None
    scheduled_for: datetime | None = None
    trigger_type: str = 'manual'
    published_count: int = 0
    held_count: int = 0
    stage: str | None = None
    log_tail: str | None = None
    error_message: str | None = None
    created_at: datetime
    finished_at: datetime | None = None


class CrawlTaskListResponse(BaseModel):
    """采集任务历史列表的分页响应。"""

    items: list[CrawlTaskOut]
    total: int
    page: int
    page_size: int


class CrawlPlatformInfo(BaseModel):
    """一个可选平台的展示信息，供前端渲染勾选项时附带提示文案。"""

    value: str
    label: str
    warning: str | None = None


class CrawlPlatformListResponse(BaseModel):
    """可用平台列表，前端加载页面时拉一次用于渲染勾选框。"""

    platforms: list[CrawlPlatformInfo]
