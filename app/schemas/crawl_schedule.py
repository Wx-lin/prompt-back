"""Validated administrator schedule settings."""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class CrawlScheduleCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    platforms: list[str] = Field(min_length=1, max_length=3)
    keyword: str = Field(min_length=1, max_length=200)
    keywords: list[str] = Field(default_factory=list, max_length=30)
    times: list[str] = Field(min_length=1, max_length=12)
    limit_count: int = Field(default=20, ge=1, le=50)
    enabled: bool = True
    auto_publish: bool = True

    @field_validator("name", "keyword")
    @classmethod
    def clean_text(cls, value):
        if value is None:
            raise ValueError("设置不能为空")
        value = value.strip()
        if not value:
            raise ValueError("不能为空")
        return value

    @field_validator("keywords")
    @classmethod
    def clean_keywords(cls, value):
        cleaned = [item.strip() for item in value]
        if any(not item or len(item) > 200 for item in cleaned):
            raise ValueError("关键词须为 1-200 个字符")
        if any("," in item for item in cleaned):
            raise ValueError("关键词不能包含英文逗号，请拆成独立列表项")
        return list(dict.fromkeys(cleaned))

    @field_validator("platforms")
    @classmethod
    def clean_platforms(cls, value):
        if value is None:
            raise ValueError("设置不能为空")
        if any(p not in {"xiaohongshu", "weibo", "kuaishou"} for p in value):
            raise ValueError("不支持的平台")
        return list(dict.fromkeys(value))

    @field_validator("times")
    @classmethod
    def clean_times(cls, value):
        if value is None:
            raise ValueError("设置不能为空")
        if any(not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", t) for t in value):
            raise ValueError("时间须使用 HH:MM 格式")
        return sorted(set(value))


class CrawlScheduleUpdate(CrawlScheduleCreate):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    platforms: list[str] | None = Field(default=None, min_length=1, max_length=3)
    keyword: str | None = Field(default=None, min_length=1, max_length=200)
    times: list[str] | None = Field(default=None, min_length=1, max_length=12)
    limit_count: int | None = Field(default=None, ge=1, le=50)
    enabled: bool | None = None
    auto_publish: bool | None = None


class CrawlScheduleOut(CrawlScheduleCreate):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
