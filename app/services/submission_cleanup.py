"""Reconcile every eligible asset with keyset pagination and bounded row-lock batches."""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select

from app.models import Prompt, SubmissionAsset
from app.services import cos_service

logger = logging.getLogger(__name__)


async def cleanup_assets(db):
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    cursor = None
    while True:
        query = select(SubmissionAsset).where(
            SubmissionAsset.prompt_id.is_(None),
            or_(SubmissionAsset.created_at < cutoff, SubmissionAsset.used_at.is_not(None)),
        )
        if cursor is not None:
            query = query.where(SubmissionAsset.id > cursor)
        assets = (
            await db.scalars(query.order_by(SubmissionAsset.id).with_for_update(skip_locked=True).limit(100))
        ).all()
        if not assets:
            break
        cursor = assets[-1].id
        for asset in assets:
            try:
                await cos_service.delete_submission_image(asset.object_key)
            except Exception:
                logger.exception("Expired submission asset cleanup will retry: %s", asset.object_key)
            else:
                await db.delete(asset)
        await db.commit()
    cursor = None
    while True:
        # Lock the Prompt too: active approval/republication transactions must not be re-privatized.
        query = (
            select(SubmissionAsset, Prompt)
            .join(Prompt, SubmissionAsset.prompt_id == Prompt.id)
            .where(Prompt.is_published.is_(False))
        )
        if cursor is not None:
            query = query.where(SubmissionAsset.id > cursor)
        rows = (
            await db.execute(query.order_by(SubmissionAsset.id).with_for_update(skip_locked=True).limit(100))
        ).all()
        if not rows:
            break
        cursor = rows[-1][0].id
        for asset, _ in rows:
            try:
                await cos_service.set_submission_visibility(asset.object_key, False)
            except Exception:
                logger.exception("Private submission ACL cleanup will retry: %s", asset.object_key)
        await db.commit()
