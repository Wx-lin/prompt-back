"""Read-only administrator insights response contracts."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class UserCounts(BaseModel):
    total: int
    enabled: int
    disabled: int
    today_new: int


class ContentCounts(BaseModel):
    total: int
    published: int
    pending: int
    rejected: int
    other_unpublished: int
    images: int


class CrawlCounts(BaseModel):
    total: int
    pending: int


class CategoryCounts(BaseModel):
    key: str
    label: str
    total: int
    published: int


class AdminDashboard(BaseModel):
    generated_at: datetime
    users: UserCounts
    content: ContentCounts
    crawl: CrawlCounts
    categories: list[CategoryCounts]


class AdminUser(BaseModel):
    id: uuid.UUID
    nickname: str | None
    email: str | None
    is_active: bool
    created_at: datetime
    last_login_at: datetime | None


class AdminUserList(BaseModel):
    items: list[AdminUser]
    total: int
    all_users_total: int
    page: int
    page_size: int
