"""Idempotent private-image cleanup with shared-asset and lease protection."""

import logging
from datetime import datetime

from sqlalchemy import exists, or_, select

from app.models import PromptAnalysis, PromptAnalysisAsset
from app.services.prompt_analysis import storage
from app.services.prompt_analysis.jobs import Sessions, _clock

logger = logging.getLogger(__name__)


def _active_lease(job: PromptAnalysis, now: datetime) -> bool:
    return bool(job.status == "running" and job.lease_expires_at and job.lease_expires_at > now)


async def _expire_job(db, job: PromptAnalysis, now: datetime) -> None:
    job.deleted_at = now
    await db.flush()
    job.generated_result = None
    job.edited_prompts = None
    job.idea_text = None
    job.purpose = None
    job.style = None
    job.user_message = None
    job.context_snapshot = None
    if job.status == "queued":
        job.status = "canceled"
        job.quota_state = "released"
        job.finished_at = now
        job.phase = "finished"


async def expire_history(sessions: Sessions) -> int:
    """Persist tombstones before clearing text, retaining idempotency/accounting."""
    count = 0
    async with sessions() as db, db.begin():
        now = await _clock(db)
        asset_ids = (
            await db.scalars(
                select(PromptAnalysis.asset_id)
                .where(
                    PromptAnalysis.deleted_at.is_(None),
                    PromptAnalysis.history_retain_until <= now,
                    PromptAnalysis.asset_id.is_not(None),
                )
                .distinct()
                .order_by(PromptAnalysis.asset_id)
                .limit(100)
            )
        ).all()
        for asset_id in asset_ids:
            asset = await db.scalar(
                select(PromptAnalysisAsset)
                .where(PromptAnalysisAsset.id == asset_id)
                .with_for_update(skip_locked=True)
            )
            if asset is None:
                continue
            jobs = (
                await db.scalars(
                    select(PromptAnalysis)
                    .where(
                        PromptAnalysis.asset_id == asset_id,
                        PromptAnalysis.deleted_at.is_(None),
                        PromptAnalysis.history_retain_until <= now,
                    )
                    .order_by(PromptAnalysis.id)
                    .with_for_update()
                )
            ).all()
            for job in jobs:
                await _expire_job(db, job, now)
                count += 1
        # Assetless text work has no object lock. Keep its tombstone/accounting,
        # clearing the original idea with the same retention as generated text.
        text_jobs = (
            await db.scalars(
                select(PromptAnalysis)
                .where(
                    PromptAnalysis.asset_id.is_(None),
                    PromptAnalysis.deleted_at.is_(None),
                    PromptAnalysis.history_retain_until <= now,
                )
                .order_by(PromptAnalysis.id)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
        ).all()
        for job in text_jobs:
            await _expire_job(db, job, now)
            count += 1
    return count


async def _mark_deleting(sessions: Sessions, asset_id) -> tuple[str, str] | None:
    async with sessions() as db, db.begin():
        asset = await db.scalar(
            select(PromptAnalysisAsset)
            .where(
                PromptAnalysisAsset.id == asset_id,
                PromptAnalysisAsset.state != "deleted",
            )
            .with_for_update(skip_locked=True)
        )
        if asset is None:
            return None
        now = await _clock(db)
        jobs = (
            await db.scalars(
                select(PromptAnalysis)
                .where(
                    PromptAnalysis.asset_id == asset_id,
                )
                .order_by(PromptAnalysis.id)
                .with_for_update()
            )
        ).all()
        if any(_active_lease(job, now) for job in jobs):
            return None
        if any(job.deleted_at is None and job.image_retain_until > now for job in jobs):
            return None
        if not jobs and asset.unbound_expires_at > now:
            return None
        if asset.purge_after is not None and asset.purge_after > now:
            return None
        asset.state = "deleting"
        return asset.object_key, asset.thumbnail_key


async def cleanup_assets(sessions: Sessions) -> int:
    """Mark under locks, delete outside transactions, retry partial failures."""
    async with sessions() as db:
        now = await _clock(db)
        references = select(PromptAnalysis.id).where(PromptAnalysis.asset_id == PromptAnalysisAsset.id)
        retained = references.where(
            PromptAnalysis.deleted_at.is_(None), PromptAnalysis.image_retain_until > now
        )
        leased = references.where(PromptAnalysis.status == "running", PromptAnalysis.lease_expires_at > now)
        asset_ids = (
            await db.scalars(
                select(PromptAnalysisAsset.id)
                .where(
                    PromptAnalysisAsset.state.in_(("uploading", "ready", "deleting")),
                    ~exists(retained),
                    ~exists(leased),
                    or_(exists(references), PromptAnalysisAsset.unbound_expires_at <= now),
                    or_(PromptAnalysisAsset.purge_after.is_(None), PromptAnalysisAsset.purge_after <= now),
                )
                .order_by(PromptAnalysisAsset.created_at, PromptAnalysisAsset.id)
                .limit(100)
            )
        ).all()
    deleted = 0
    for asset_id in asset_ids:
        keys = await _mark_deleting(sessions, asset_id)
        if keys is None:
            continue
        try:
            for object_key in keys:
                await storage.delete_private(object_key)
        except Exception:
            # Deliberately omit provider error text and object keys from logs.
            logger.warning("Private analysis cleanup deferred asset_id=%s", asset_id)
            continue
        async with sessions() as db, db.begin():
            asset = await db.scalar(
                select(PromptAnalysisAsset)
                .where(
                    PromptAnalysisAsset.id == asset_id,
                )
                .with_for_update()
            )
            if asset.state == "deleting":
                asset.state = "deleted"
                deleted += 1
    return deleted
