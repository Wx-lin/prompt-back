"""One search-console export per engine/dimension/nonoverlapping window."""

from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SeoSnapshot(Base):
    __tablename__ = "seo_snapshots"
    __table_args__ = (
        UniqueConstraint("engine", "dimension", "start_date", "end_date", name="uq_seo_snapshot_window"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    engine: Mapped[str] = mapped_column(String(10), index=True)
    dimension: Mapped[str] = mapped_column(String(10))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    rows: Mapped[list] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
