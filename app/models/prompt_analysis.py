"""Private image analysis assets, durable jobs and fenced dispatch attempts."""

import uuid
from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.database import Base


class PromptAnalysisAsset(Base):
    __tablename__ = "prompt_analysis_assets"
    __table_args__ = (
        UniqueConstraint("id", "user_id", name="uq_prompt_analysis_asset_owner"),
        CheckConstraint(
            "state IN ('uploading','ready','deleting','deleted')", name="ck_prompt_analysis_asset_state"
        ),
        CheckConstraint(
            "width > 0 AND height > 0 AND byte_size > 0", name="ck_prompt_analysis_asset_dimensions"
        ),
        Index("ix_prompt_analysis_assets_expiry", "state", "unbound_expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    object_key: Mapped[str] = mapped_column(String(250), unique=True)
    thumbnail_key: Mapped[str] = mapped_column(String(250), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    byte_size: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(20), default="uploading", server_default="uploading")
    unbound_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PromptAnalysis(Base):
    __tablename__ = "prompt_analyses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["asset_id", "user_id"],
            ["prompt_analysis_assets.id", "prompt_analysis_assets.user_id"],
            name="fk_prompt_analysis_asset_owner",
        ),
        UniqueConstraint("user_id", "idempotency_key", name="uq_prompt_analysis_idempotency"),
        UniqueConstraint("id", "user_id", name="uq_prompt_analysis_owner"),
        UniqueConstraint("conversation_id", "conversation_turn", name="uq_prompt_conversation_turn"),
        ForeignKeyConstraint(
            ["conversation_id", "user_id"], ["prompt_analyses.id", "prompt_analyses.user_id"],
            name="fk_prompt_conversation_owner",
        ),
        ForeignKeyConstraint(
            ["parent_analysis_id", "user_id"], ["prompt_analyses.id", "prompt_analyses.user_id"],
            name="fk_prompt_parent_owner",
        ),
        CheckConstraint(
            "conversation_turn IS NULL OR conversation_turn >= 1",
            name="ck_prompt_conversation_turn",
        ),
        CheckConstraint(
            "user_message IS NULL OR char_length(user_message) <= 500",
            name="ck_prompt_conversation_message",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','unknown','canceled')",
            name="ck_prompt_analysis_status",
        ),
        CheckConstraint(
            "quota_state IN ('reserved','consumed','released','uncertain')",
            name="ck_prompt_analysis_quota_state",
        ),
        CheckConstraint("revision >= 0", name="ck_prompt_analysis_revision"),
        CheckConstraint(
            "(input_type = 'image' AND asset_id IS NOT NULL AND idea_text IS NULL "
            "AND purpose IS NULL AND style IS NULL) OR "
            "(input_type = 'text' AND asset_id IS NULL AND "
            "((idea_text IS NOT NULL AND char_length(idea_text) BETWEEN 1 AND 500 "
            "AND char_length(trim(idea_text)) > 0) OR "
            "(deleted_at IS NOT NULL AND idea_text IS NULL)))",
            name="ck_prompt_analysis_input",
        ),
        CheckConstraint(
            "purpose IS NULL OR purpose IN ('wallpaper','avatar','poster','product','illustration')",
            name="ck_prompt_analysis_purpose",
        ),
        CheckConstraint(
            "style IS NULL OR style IN ('cinematic','photorealistic','illustration','anime','minimalist')",
            name="ck_prompt_analysis_style",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR deleted_at IS NOT NULL OR generated_result IS NOT NULL",
            name="ck_prompt_analysis_success_result",
        ),
        Index("ix_prompt_analyses_history", "user_id", "created_at", "id"),
        Index("ix_prompt_analyses_queue", "status", "created_at"),
        Index("ix_prompt_analyses_lease", "status", "lease_expires_at"),
        Index("ix_prompt_analyses_asset", "asset_id"),
        Index("ix_prompt_analyses_conversation", "user_id", "conversation_id", "conversation_turn"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"))
    asset_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    input_type: Mapped[str] = mapped_column(String(10), default="image", server_default="image")
    idea_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    purpose: Mapped[str | None] = mapped_column(String(30), nullable=True)
    style: Mapped[str | None] = mapped_column(String(30), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    parent_analysis_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    conversation_turn: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_snapshot: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    source_client: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="queued", server_default="queued")
    phase: Mapped[str] = mapped_column(String(30), default="queued", server_default="queued")
    prompt_version: Mapped[str] = mapped_column(String(100))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(150))
    config_snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)
    generated_result: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    edited_prompts: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(80))
    quota_date: Mapped[date] = mapped_column(Date)
    quota_state: Mapped[str] = mapped_column(String(20), default="reserved", server_default="reserved")
    lease_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    image_retain_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PromptAnalysisAttempt(Base):
    __tablename__ = "prompt_analysis_attempts"
    __table_args__ = (
        UniqueConstraint("analysis_id", "attempt_no", name="uq_prompt_analysis_attempt_no"),
        CheckConstraint("attempt_no >= 1 AND attempt_no <= 2", name="ck_prompt_analysis_attempt_no"),
        CheckConstraint(
            "dispatch_state IN ('dispatch_intent','succeeded','failed','unknown')",
            name="ck_prompt_analysis_attempt_state",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    analysis_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("prompt_analyses.id"), index=True
    )
    attempt_no: Mapped[int] = mapped_column(Integer)
    dispatch_state: Mapped[str] = mapped_column(String(30))
    provider_request_id: Mapped[str | None] = mapped_column(String(200))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(80))
    dispatched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
