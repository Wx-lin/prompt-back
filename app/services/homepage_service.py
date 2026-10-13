"""Atomic, optimistic homepage curation; only publicly available works are exposed."""

import hashlib
import uuid

from sqlalchemy import case, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Prompt, PromptImage
from app.schemas.homepage import FeaturedResponse
from app.schemas.prompt import PromptListItem


class FeaturedConflict(Exception):
    pass


class InvalidFeaturedSelection(Exception):
    pass


def queue_revision(ids: list[uuid.UUID]) -> str:
    return hashlib.sha256(",".join(str(identifier) for identifier in ids).encode()).hexdigest()


def thumbnail_for_prompt(row: Prompt) -> str | None:
    image = next((image for image in row.images if image.image_url.strip()), None)
    if image is None:
        return None
    return (image.thumbnail_url or "").strip() or image.image_url.strip()


def visible_featured_query():
    return (
        select(Prompt)
        .where(
            Prompt.homepage_rank.is_not(None),
            Prompt.is_published.is_(True),
            Prompt.images.any(func.trim(PromptImage.image_url) != ""),
        )
        .order_by(Prompt.homepage_rank, Prompt.id)
    )


async def get_featured(db: AsyncSession) -> FeaturedResponse:
    rows = (await db.execute(visible_featured_query().options(selectinload(Prompt.images)))).scalars().all()
    return FeaturedResponse(
        items=[
            PromptListItem(
                id=row.id,
                title=row.title,
                category=row.category,
                models=row.models,
                thumbnail_url=thumbnail_for_prompt(row),
                like_count=row.like_count,
                view_count=row.view_count,
                author_name=row.author_name,
                created_at=row.created_at,
            )
            for row in rows
        ],
        revision=queue_revision([row.id for row in rows]),
    )


async def replace_featured(
    db: AsyncSession,
    prompt_ids: list[uuid.UUID],
    revision: str,
) -> FeaturedResponse:
    # A transaction-scoped PostgreSQL lock serializes editors until commit/rollback.
    await db.execute(text("SELECT pg_advisory_xact_lock(731041302)"))
    current = await get_featured(db)
    if current.revision != revision:
        raise FeaturedConflict("精选已被修改，请刷新后重试")
    if prompt_ids:
        rows = (
            (
                await db.execute(
                    select(Prompt)
                    .where(
                        Prompt.id.in_(prompt_ids),
                        Prompt.is_published.is_(True),
                        Prompt.images.any(func.trim(PromptImage.image_url) != ""),
                    )
                    .with_for_update()
                )
            )
            .scalars()
            .all()
        )
        if {row.id for row in rows} != set(prompt_ids):
            raise InvalidFeaturedSelection("只能选择已公开且有图片的作品")
    await db.execute(update(Prompt).where(Prompt.homepage_rank.is_not(None)).values(homepage_rank=None))
    if prompt_ids:
        ranks = {identifier: index for index, identifier in enumerate(prompt_ids)}
        await db.execute(
            update(Prompt)
            .where(Prompt.id.in_(prompt_ids))
            .values(
                homepage_rank=case(ranks, value=Prompt.id),
            )
        )
    await db.commit()
    return await get_featured(db)
