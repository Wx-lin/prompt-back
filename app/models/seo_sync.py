"""Automatic source is isolated from all existing manual report snapshots."""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SeoApiDay(Base):
    __tablename__ = "seo_api_days"
    __table_args__ = (UniqueConstraint("day", "dimension", name="uq_seo_api_day_dimension"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    dimension: Mapped[str] = mapped_column(String(10))
    rows: Mapped[list] = mapped_column(JSON)
    truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SeoSyncState(Base):
    __tablename__ = "seo_sync_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    status: Mapped[str] = mapped_column(String(16))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(40))
    data_through: Mapped[date | None] = mapped_column(Date)
