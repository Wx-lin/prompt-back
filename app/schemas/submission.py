"""User submission contracts; publication and identity are server-owned."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.utils.category_catalog import CATEGORY_LABELS, CategoryKey
from app.utils.model_labels import normalize_model_labels


class SubmissionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    prompt_text: str = Field(min_length=1, max_length=20_000)
    description: str | None = Field(default=None, max_length=5000)
    category: str
    models: list[str] = Field(min_length=1, max_length=10)
    upload_ids: list[uuid.UUID] = Field(min_length=1, max_length=6)

    @field_validator("category")
    @classmethod
    def valid_category(cls, value):
        if value not in CATEGORY_LABELS:
            raise ValueError("请选择有效分类")
        return value

    @field_validator("models")
    @classmethod
    def valid_models(cls, values):
        values = normalize_model_labels(values)
        if not values or any(not value.strip() or len(value.strip()) > 50 for value in values):
            raise ValueError("请选择有效模型版本")
        return list(dict.fromkeys(value.strip() for value in values))

    @field_validator("upload_ids")
    @classmethod
    def unique_uploads(cls, values):
        if len(set(values)) != len(values):
            raise ValueError("图片不能重复")
        return values


class SubmissionOut(BaseModel):
    id: uuid.UUID
    title: str
    prompt_text: str
    description: str | None
    category: str
    models: list[str]
    author_name: str | None
    review_status: Literal["pending", "approved", "rejected"]
    published: bool
    image_urls: list[str]
    review_note: str | None
    created_at: datetime


class SubmissionList(BaseModel):
    items: list[SubmissionOut]
    total: int
    page: int
    page_size: int
    has_more: bool


class SubmissionUploadOut(BaseModel):
    id: uuid.UUID
    image_url: str


class SubmissionReject(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    note: str = Field(min_length=1, max_length=1000)


class SubmissionApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: CategoryKey | None = None
