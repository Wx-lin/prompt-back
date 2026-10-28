"""Short PostgreSQL transactions fence every potentially billable dispatch.

V1 deliberately performs no automatic provider retries. A durable dispatch intent
always makes crash recovery uncertain; it never authorizes another model call.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.models import PromptAnalysis, PromptAnalysisAsset, PromptAnalysisAttempt
from app.services.vision.conversation_adapter import CHAT_PROMPT_VERSION
from app.services.vision.text_adapter import TEXT_PROMPT_VERSION

Sessions = async_sessionmaker[AsyncSession]


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    lease_token: uuid.UUID
    object_key: str | None
    started_at: datetime
    input_type: str = "image"
    idea_text: str | None = None
    purpose: str | None = None
    style: str | None = None
    conversation_id: uuid.UUID | None = None
    user_message: str | None = None
    context_snapshot: dict | None = None
    conversation_mode: str = "auto"


async def _clock(db: AsyncSession) -> datetime:
    return await db.scalar(select(func.clock_timestamp()))


def _valid_lease(job: PromptAnalysis | None, token: uuid.UUID, now: datetime) -> bool:
    return bool(
        job
        and job.status == "running"
        and job.lease_token == token
        and job.lease_expires_at
        and job.lease_expires_at > now
    )


def _terminal(job: PromptAnalysis, now: datetime, *, status: str, quota: str, error: str | None) -> None:
    job.status = "canceled" if job.deleted_at else status
    job.phase = "finished"
    job.quota_state = quota
    job.error_code = error
    job.finished_at = now
    job.lease_token = None
    job.lease_expires_at = None
    job.image_retain_until = now + timedelta(days=30 if status == "succeeded" else 1)
    # A conversation's root deadline is a hard privacy cap, never renewed by
    # continued generation/retries or their later completion timestamps.
    if not job.conversation_id:
        job.history_retain_until = now + timedelta(days=30)
    if job.deleted_at:
        job.generated_result = None
        job.edited_prompts = None
        job.idea_text = None
        job.purpose = None
        job.style = None
        job.user_message = None
        job.context_snapshot = None


async def claim_job(sessions: Sessions) -> ClaimedJob | None:
    """Claim with SKIP LOCKED, committing before private storage/model I/O."""
    settings = get_settings()
    async with sessions() as db:
        candidates = (
            await db.execute(
                select(PromptAnalysis.id, PromptAnalysis.asset_id)
                .where(PromptAnalysis.status == "queued")
                .order_by(PromptAnalysis.created_at, PromptAnalysis.id)
                .limit(100)
            )
        ).all()
    for job_id, asset_id in candidates:
        # A separate transaction per candidate avoids holding several assets
        # in queue order while cleanup takes them in asset-ID order.
        async with sessions() as db, db.begin():
            # Shared lock order: assets first, then analyses. Never wait on a
            # busy asset while holding a different analysis lock.
            asset = None
            if asset_id is not None:
                asset = await db.scalar(
                    select(PromptAnalysisAsset)
                    .where(PromptAnalysisAsset.id == asset_id)
                    .with_for_update(skip_locked=True)
                )
                if asset is None:
                    continue
            job = await db.scalar(
                select(PromptAnalysis)
                .where(PromptAnalysis.id == job_id, PromptAnalysis.status == "queued")
                .with_for_update(skip_locked=True)
            )
            if job is None:
                continue
            now = await _clock(db)
            if job.deleted_at:
                _terminal(job, now, status="canceled", quota="released", error=None)
            elif job.created_at + timedelta(seconds=settings.prompt_analysis_queue_ttl_seconds) <= now:
                _terminal(job, now, status="failed", quota="released", error="queue_expired")
            elif job.input_type == "image" and (
                asset is None or asset.state != "ready" or job.image_retain_until <= now
            ):
                _terminal(job, now, status="failed", quota="released", error="image_unavailable")
            elif job.input_type == "text" and not job.idea_text:
                _terminal(job, now, status="failed", quota="released", error="input_unavailable")
            else:
                token = uuid.uuid4()
                job.status = "running"
                job.phase = "preparing"
                job.lease_token = token
                job.lease_expires_at = now + timedelta(seconds=settings.prompt_analysis_lease_seconds)
                job.started_at = now
                return ClaimedJob(
                    job.id, token, asset.object_key if asset else None, now,
                    job.input_type, job.idea_text, job.purpose, job.style,
                    job.conversation_id, job.user_message, job.context_snapshot,
                    job.config_snapshot.get("conversation_mode", "auto"),
                )
    return None


async def dispatch_job(sessions: Sessions, job_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
    """Persist intent exactly once BEFORE external I/O, with asset/lease fencing."""
    settings = get_settings()
    async with sessions() as db, db.begin():
        record = (
            await db.execute(select(PromptAnalysis.asset_id).where(PromptAnalysis.id == job_id))
        ).first()
        if record is None:
            return False
        asset_id = record.asset_id
        asset = None
        if asset_id is not None:
            asset = await db.scalar(
                select(PromptAnalysisAsset).where(PromptAnalysisAsset.id == asset_id).with_for_update()
            )
        job = await db.scalar(select(PromptAnalysis).where(PromptAnalysis.id == job_id).with_for_update())
        now = await _clock(db)
        if not _valid_lease(job, lease_token, now) or job.deleted_at:
            return False
        if job.input_type == "image" and (asset is None or asset.state != "ready"):
            return False
        if job.input_type == "text" and not job.idea_text:
            _terminal(job, now, status="failed", quota="released", error="input_unavailable")
            return False
        expected = {
            "provider": settings.prompt_vision_provider,
            "model": settings.prompt_vision_model,
            "prompt_version": (
                CHAT_PROMPT_VERSION if job.conversation_id else
                TEXT_PROMPT_VERSION if job.input_type == "text" else settings.prompt_analysis_prompt_version
            ),
            "max_tokens": settings.prompt_vision_max_tokens,
            "base_url": settings.prompt_vision_base_url,
            "enable_thinking": False if settings.prompt_vision_provider == "dashscope" else None,
        }
        snapshot_changed = any(
            key in job.config_snapshot and job.config_snapshot[key] != value
            for key, value in expected.items()
        )
        if snapshot_changed or any(
            getattr(job, name) != expected[name] for name in ("provider", "model", "prompt_version")
        ):
            _terminal(job, now, status="failed", quota="released", error="configuration_changed")
            return False
        if job.input_type == "image" and job.image_retain_until <= now:
            _terminal(job, now, status="failed", quota="released", error="image_unavailable")
            return False
        if job.created_at + timedelta(seconds=settings.prompt_analysis_queue_ttl_seconds) <= now:
            _terminal(job, now, status="failed", quota="released", error="queue_expired")
            return False
        attempts = (
            await db.scalars(select(PromptAnalysisAttempt).where(PromptAnalysisAttempt.analysis_id == job_id))
        ).all()
        if attempts:
            return False
        if job.started_at + timedelta(seconds=settings.prompt_analysis_execution_timeout) <= now:
            _terminal(job, now, status="failed", quota="released", error="execution_timeout")
            return False
        db.add(
            PromptAnalysisAttempt(
                analysis_id=job_id,
                attempt_no=1,
                dispatch_state="dispatch_intent",
                dispatched_at=now,
            )
        )
        job.phase = "generating" if job.input_type == "text" else "analyzing"
        return True


async def heartbeat_job(sessions: Sessions, job_id: uuid.UUID, lease_token: uuid.UUID) -> bool:
    settings = get_settings()
    async with sessions() as db, db.begin():
        job = await db.scalar(select(PromptAnalysis).where(PromptAnalysis.id == job_id).with_for_update())
        now = await _clock(db)
        if not _valid_lease(job, lease_token, now):
            return False
        deadline = job.started_at + timedelta(seconds=settings.prompt_analysis_execution_timeout)
        if deadline <= now:
            return False
        job.lease_expires_at = min(
            now + timedelta(seconds=settings.prompt_analysis_lease_seconds),
            deadline,
        )
        return True


def _result_parts(result, *, allow_reply=False):
    from app.schemas.prompt_analysis import ModelResult
    from app.schemas.prompt_conversation import ConversationReplyResult

    content = getattr(result, "result", result)
    if isinstance(content, (ModelResult, ConversationReplyResult)):
        content = content.model_dump(mode="json")
    schema = (
        ConversationReplyResult
        if allow_reply and isinstance(content, dict) and content.get("schema_version") == "reply-1"
        else ModelResult
    )
    validated = schema.model_validate(content).model_dump(mode="json")
    provider_id = getattr(result, "provider_request_id", None)
    raw_usage = getattr(result, "usage", {})
    # Usage is numeric accounting only, never arbitrary provider response text.
    usage = {
        key: value
        for key, value in raw_usage.items()
        if key in {
            "input_tokens", "output_tokens", "prompt_tokens", "completion_tokens", "total_tokens",
            "image_tokens", "text_input_tokens", "cached_tokens", "reasoning_tokens", "text_output_tokens",
        }
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    }
    return validated, str(provider_id)[:200] if provider_id else None, usage


async def finish_job(
    sessions: Sessions,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    *,
    result=None,
    error_code: str | None = None,
    uncertain: bool = False,
) -> bool:
    """Atomically store a locally validated result and its successful state."""
    async with sessions() as db, db.begin():
        job = await db.scalar(select(PromptAnalysis).where(PromptAnalysis.id == job_id).with_for_update())
        now = await _clock(db)
        if not _valid_lease(job, lease_token, now):
            return False
        try:
            parts = _result_parts(
                result,
                allow_reply=bool(job.conversation_id)
                and job.config_snapshot.get("conversation_mode", "auto") == "auto",
            ) if result is not None else None
        except (ValueError, TypeError):
            # Invalid output is a known failure, not a persistence outage or a
            # reason to send another provider request.
            parts, error_code, uncertain = None, "model_invalid_output", False
        attempt = await db.scalar(
            select(PromptAnalysisAttempt)
            .where(PromptAnalysisAttempt.analysis_id == job_id)
            .order_by(PromptAnalysisAttempt.attempt_no.desc())
            .limit(1)
        )
        if parts is not None and attempt is None:
            return False
        status = "succeeded" if parts is not None else ("unknown" if uncertain else "failed")
        quota = "uncertain" if uncertain else ("consumed" if attempt is not None else "released")
        if parts is not None and not job.deleted_at:
            job.generated_result = parts[0]
        _terminal(job, now, status=status, quota=quota, error=error_code)
        if attempt is not None:
            attempt.dispatch_state = status
            attempt.error_code = error_code
            attempt.finished_at = now
            if parts is not None:
                attempt.provider_request_id = parts[1]
                attempt.usage = parts[2]
        return True


async def recover_jobs(sessions: Sessions) -> int:
    """Expire queues without provider access; recover only unsent abandoned work."""
    settings = get_settings()
    async with sessions() as db:
        now = await _clock(db)
        candidates = (
            await db.scalars(
                select(PromptAnalysis.id)
                .where(
                    or_(
                        and_(PromptAnalysis.status == "running", PromptAnalysis.lease_expires_at <= now),
                        and_(
                            PromptAnalysis.status == "queued",
                            or_(
                                PromptAnalysis.created_at
                                <= now - timedelta(seconds=settings.prompt_analysis_queue_ttl_seconds),
                                PromptAnalysis.deleted_at.is_not(None),
                            ),
                        ),
                    )
                )
                .order_by(PromptAnalysis.id)
                .limit(100)
            )
        ).all()
    recovered = 0
    for job_id in candidates:
        # One row per transaction prevents a lock cycle with multi-asset expiry.
        async with sessions() as db, db.begin():
            job = await db.scalar(
                select(PromptAnalysis)
                .where(
                    PromptAnalysis.id == job_id,
                )
                .with_for_update(skip_locked=True)
            )
            now = await _clock(db)
            if job is None:
                continue
            if job.status == "queued":
                if job.deleted_at:
                    _terminal(job, now, status="canceled", quota="released", error=None)
                elif job.created_at + timedelta(seconds=settings.prompt_analysis_queue_ttl_seconds) <= now:
                    _terminal(job, now, status="failed", quota="released", error="queue_expired")
                else:
                    continue
            elif job.status == "running" and job.lease_expires_at and job.lease_expires_at <= now:
                attempt = await db.scalar(
                    select(PromptAnalysisAttempt)
                    .where(
                        PromptAnalysisAttempt.analysis_id == job.id,
                    )
                    .order_by(PromptAnalysisAttempt.attempt_no.desc())
                    .limit(1)
                )
                if attempt is not None:
                    _terminal(job, now, status="unknown", quota="uncertain", error="worker_lost")
                    attempt.dispatch_state = "unknown"
                    attempt.error_code = "worker_lost"
                    attempt.finished_at = now
                elif job.deleted_at:
                    _terminal(job, now, status="canceled", quota="released", error=None)
                else:
                    job.status = "queued"
                    job.phase = "queued"
                    job.lease_token = None
                    job.lease_expires_at = None
                    job.started_at = None
            else:
                continue
            recovered += 1
    return recovered
