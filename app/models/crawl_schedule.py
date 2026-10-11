"""Persisted crawl schedules; times are interpreted in Asia/Shanghai."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class CrawlSchedule(Base):
    __tablename__ = "crawl_schedules"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100))
    platforms: Mapped[list[str]] = mapped_column(JSON)
    keyword: Mapped[str] = mapped_column(String(200))
    times: Mapped[list[str]] = mapped_column(JSON)
    limit_count: Mapped[int] = mapped_column(Integer, default=20)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
