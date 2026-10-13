"""Owned user submissions and serialized manual review."""

import logging
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Prompt, PromptImage, SubmissionAsset, User
from app.schemas.submission import SubmissionCreate, SubmissionOut
from app.services import cos_service
from app.utils.model_labels import normalize_model_labels

logger = logging.getLogger(__name__)


class SubmissionConflict(ValueError):
    pass


async def create_submission(db: AsyncSession, user: User, body: SubmissionCreate) -> Prompt:
    assets = list(
        (
            await db.scalars(
                select(SubmissionAsset)
                .where(SubmissionAsset.id.in_(body.upload_ids))
                .order_by(SubmissionAsset.id)
                .with_for_update()
            )
        ).all()
    )
    cutoff = datetime.now(UTC) - timedelta(hours=24)
    if len(assets) != len(body.upload_ids) or any(
        asset.user_id != user.id or asset.used_at is not None or asset.created_at < cutoff for asset in assets
    ):
        raise SubmissionConflict("图片不存在、已使用或已过期，请重新上传")
    prompt = Prompt(
        title=body.title,
        prompt_text=body.prompt_text,
        description=body.description,
        category=body.category,
        models=normalize_model_labels(body.models),
        author_name=user.nickname or "创作者",
        author_avatar_url=user.avatar_url,
        submitter_id=user.id,
        review_status="pending",
        is_published=False,
    )
    db.add(prompt)
    await db.flush()
    by_id = {asset.id: asset for asset in assets}
    for order, asset_id in enumerate(body.upload_ids):
        asset = by_id[asset_id]
        asset.prompt_id = prompt.id
        asset.used_at = datetime.now(UTC)
        db.add(
            PromptImage(
                prompt_id=prompt.id,
                image_url=asset.image_url,
                thumbnail_url=asset.image_url,
                sort_order=order,
            )
        )
    await db.commit()
    await db.refresh(prompt)
    return prompt


async def list_submissions(
    db: AsyncSession, user_id: uuid.UUID | None, status: str | None, page: int, page_size: int
):
    query = select(Prompt).where(Prompt.review_status.is_not(None))
    if user_id is not None:
        query = query.where(Prompt.submitter_id == user_id)
    if status is not None:
        query = query.where(Prompt.review_status == status)
    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    rows = (
        await db.scalars(
            query.order_by(Prompt.created_at.desc(), Prompt.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return list(rows), total


async def to_out(db: AsyncSession, prompt: Prompt) -> SubmissionOut:
    assets = (await db.scalars(select(SubmissionAsset).where(SubmissionAsset.prompt_id == prompt.id))).all()
    keyed = {asset.image_url: asset.object_key for asset in assets}
    images = (
        await db.scalars(
            select(PromptImage).where(PromptImage.prompt_id == prompt.id).order_by(PromptImage.sort_order)
        )
    ).all()
    return SubmissionOut(
        id=prompt.id,
        title=prompt.title,
        prompt_text=prompt.prompt_text,
        description=prompt.description,
        category=prompt.category,
        models=normalize_model_labels(prompt.models),
        author_name=prompt.author_name,
        review_status=prompt.review_status,
        published=prompt.is_published,
        review_note=prompt.review_note,
        created_at=prompt.created_at,
        image_urls=[
            image.image_url
            if prompt.is_published
            else cos_service.submission_preview_url(keyed[image.image_url])
            for image in images
            if prompt.is_published or image.image_url in keyed
        ],
    )


async def review_submission(
    db: AsyncSession, prompt_id: uuid.UUID, approved: bool, note: str | None = None
) -> Prompt | None:
    prompt = await db.scalar(
        select(Prompt).where(Prompt.id == prompt_id, Prompt.review_status.is_not(None)).with_for_update()
    )
    if prompt is None:
        return None
    if prompt.review_status != "pending":
        raise SubmissionConflict("作品已审核，请刷新列表")
    assets = (await db.scalars(select(SubmissionAsset).where(SubmissionAsset.prompt_id == prompt.id))).all()
    keys = [asset.object_key for asset in assets]
    if approved:
        # Only publish after all images become readable; failed reviews keep their pending state.
        try:
            for asset in assets:
                await cos_service.set_submission_visibility(asset.object_key, True)
        except Exception:
            await restore_private(keys)
            raise
    prompt.review_status = "approved" if approved else "rejected"
    prompt.review_note = note
    prompt.reviewed_at = datetime.now(UTC)
    prompt.is_published = approved
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        if approved:
            await restore_private(keys)
        raise
    await db.refresh(prompt)
    return prompt


async def restore_private(keys: list[str]):
    # A timeout can follow a successful remote ACL change. Reconcile every attempted key.
    for key in keys:
        try:
            await cos_service.set_submission_visibility(key, False)
        except Exception:
            logger.exception("Private submission ACL reconciliation needed for %s", key)
