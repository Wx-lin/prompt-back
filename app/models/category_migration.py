"""Persistent legacy membership and optimistic migration audit."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class CategoryLegacyMembership(Base):
    __tablename__ = "category_legacy_memberships"
    legacy_key: Mapped[str] = mapped_column(String(50), primary_key=True)
    prompt_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompts.id", ondelete="CASCADE"), primary_key=True
    )


class CategoryMigrationChange(Base):
    __tablename__ = "category_migration_changes"
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    entity: Mapped[str] = mapped_column(String(20), primary_key=True)
    record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    before_state: Mapped[dict] = mapped_column(JSON, nullable=False)
    after_state: Mapped[dict] = mapped_column(JSON, nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    rolled_back_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
