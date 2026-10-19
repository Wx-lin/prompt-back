"""Anonymous browser hashes only; no raw identifiers or request metadata."""

from datetime import date, datetime

from sqlalchemy import CheckConstraint, Date, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class VisitorDaily(Base):
    __tablename__ = "visitor_daily"
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    visitor_hash: Mapped[str] = mapped_column(String(64), primary_key=True)


class TrafficTrackingState(Base):
    __tablename__ = "traffic_tracking_state"
    __table_args__ = (CheckConstraint("id = 1", name="ck_traffic_tracking_singleton"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
