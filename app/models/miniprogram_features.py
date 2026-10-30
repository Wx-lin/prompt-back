"""Persistent configuration for the gallery mini-program."""

from sqlalchemy import Boolean, CheckConstraint, Integer, false
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MiniProgramFeatures(Base):
    __tablename__ = "miniprogram_features"
    __table_args__ = (CheckConstraint("id = 1", name="ck_miniprogram_features_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creation_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=false())
